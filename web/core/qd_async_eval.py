"""Phase 4: Async QD evaluation — fire-and-forget background fitness eval.

Mirrors the exploitability-probe fire-and-forget pattern in
generation_scheduler.post_generation_cleanup (the well-tested template, commit
history documented there). Key design properties preserved:

  1. DIRECT CODE-LAYER CALL (not an MCP tool). The Orchestrator LLM is not
     forced to call any tool, so any evolution-driven side-effect that MUST run
     per generation has to be a direct call. (This is the structural root cause
     of the original "probe never ran" bug.)
  2. SINGLE-FLIGHT guard (_qd_eval_running) so overlapping fast crossover gens
     cannot pile up QD eval threads or race on behavior_archive.json.
  3. DAEMON THREAD — post_generation_cleanup returns immediately; the worker
     writes behavior_archive.json (~1 generation latency, acceptable for a
     lagging diversity signal).
  4. WORKER USES ONLY logging + log_system_event (both thread-safe: fcntl file
     write + EventBroadcaster.call_soon_threadsafe). It NEVER touches
     ui.log_history (asyncio.Queue race from a non-loop thread).
  5. CANARY event in the main thread BEFORE launch (mechanical guarantee the
     block was reached).
  6. CANCEL FLAG checked between opponents so a shutdown can break the worker
     cleanly (no half-written archive: write_locked_json is atomic).
  7. TIMEOUT: a watchdog sets the cancel flag after ASYNC_EVAL_TIMEOUT_SEC; the
     worker breaks at the next opponent boundary, so outstanding -> 0 is bounded.

Outstanding->0 guarantee (two layers):
  - single-flight: at most ONE outstanding QD eval thread at a time.
  - daemon thread: on process exit the thread is reaped (cannot block shutdown).
    The cancel flag + opponent-boundary break ensures the worker emits a
    terminal (done/failed/cancelled) event promptly even mid-run.
"""

import json
import logging
import threading
import time
import traceback
from typing import Optional

log = logging.getLogger("pok.qd_async")

# Module-level single-flight guard (atomic on a single event loop, same pattern
# as generation_scheduler._probe_running). Do NOT share with _probe_running —
# QD eval and exploitability probe are independent background tasks and should
# not serialize each other.
_qd_eval_running = threading.Event()

# Cancel flag set by cancel_qd_eval (shutdown) or the per-launch watchdog timer.
_qd_cancel = threading.Event()

# WHY a reason is tracked (root-cause fix for QD eval 100% cancellation, 2026-06-18):
# the old code used ONE flag for BOTH shutdown-cancel and watchdog-timeout. Post-eval
# could not tell them apart, so it discarded results on either — but a watchdog
# timeout fires AFTER the eval already produced usable fitness samples. Discarding
# them caused 100% cancellation (20/20 after the e037548 restart) and ZERO k3
# archive entries. Now: shutdown discards (process exiting); watchdog keeps result.
_qd_cancel_reason = None  # None | "shutdown" | "watchdog"
_qd_cancel_reason_lock = threading.Lock()

ASYNC_EVAL_TIMEOUT_SEC = 7200  # 120 min. RAISED from 40min: real QD k=3 load
                               # (3 opponents × k=3 × n_games=8 = 72 mirror pairs)
                               # takes 40-55min under daemon resource contention;
                               # the old 40min watchdog fired mid-eval every time.
                               # The watchdog is a stuck-worker backstop, NOT a
                               # normal budget — it must sit well above real load.

_QD_EVENT_START = "pipeline.qd_eval_start"
_QD_EVENT_DONE = "pipeline.qd_eval_done"
_QD_EVENT_FAILED = "pipeline.qd_eval_failed"
_QD_EVENT_CANCELLED = "pipeline.qd_eval_cancelled"
_QD_EVENT_SKIPPED = "pipeline.qd_eval_skipped"


def _read_system_events_tail(max_lines: int = 2000):
    """Read the tail of system_events.jsonl (best-effort). Used for outstanding
    telemetry. Same pattern as generation_scheduler._read_source_v_history."""
    try:
        from system_log import SYSTEM_EVENTS_FILE
        if not SYSTEM_EVENTS_FILE.exists():
            return []
        with open(SYSTEM_EVENTS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-max_lines:]
        out = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out
    except Exception:
        return []


def outstanding_async_tasks() -> int:
    """Telemetry: count of QD eval starts without a matching terminal event.

    Terminal events: qd_eval_done, qd_eval_failed, qd_eval_cancelled. A start is
    matched by the LATEST terminal event whose ts >= start ts. Unmatched starts
    (within the events tail window) are outstanding.

    NOTE: this is advisory telemetry. The authoritative single-flight guarantee
    is _qd_eval_running (in-process). This function counts starts across process
    restarts (a crashed process leaves a dangling start; the count surfaces it).
    """
    events = _read_system_events_tail()
    starts = [e for e in events if e.get("type") == _QD_EVENT_START]
    terminals = [
        e for e in events
        if e.get("type") in (_QD_EVENT_DONE, _QD_EVENT_FAILED, _QD_EVENT_CANCELLED)
    ]
    # Greedily match each terminal to the earliest unmatched start with ts <= terminal ts.
    unmatched = list(starts)
    for term in terminals:
        term_ts = term.get("ts", 0)
        for i, st in enumerate(unmatched):
            if st.get("ts", 0) <= term_ts:
                unmatched.pop(i)
                break
    return len(unmatched)


def cancel_qd_eval() -> None:
    """Best-effort cancel: set the cancel flag. The worker checks it between
    opponents and breaks promptly. A daemon thread cannot be force-killed, so
    this is cooperative — but the worker breaks at the next opponent boundary,
    which is bounded by a single mirror_battle(n_games small) call.

    Marks the reason as "shutdown" so post-eval knows to DISCARD the result
    (the process is exiting; writing the archive is pointless/unsafe). This
    contrasts with a watchdog timeout, which KEEPS the result."""
    global _qd_cancel_reason
    with _qd_cancel_reason_lock:
        _qd_cancel_reason = "shutdown"
    _qd_cancel.set()


def _select_eval_opponents(source_v: int, max_opponents: int = 3):
    """Pick a small set of opponents for QD k=3 evaluation of a candidate.

    Preference order: the parent (source_v), then top-rated active bots. Returns
    a list of absolute main.py paths. Best-effort: returns [] on any failure.
    """
    try:
        from evolution_infra import get_bot_dir, get_active_bots, load_ratings, find_latest_active_v
        paths = []
        # Parent first.
        parent_dir = get_bot_dir(source_v)
        parent_main = parent_dir / "main.py"
        if parent_main.exists():
            paths.append(str(parent_main))
        # Top-rated active bots (by conservative rating), excluding the parent.
        try:
            active = get_active_bots() or []
        except Exception:
            active = []
        try:
            ratings = load_ratings() or {}
        except Exception:
            ratings = {}
        # Pair name -> (conservative_rating r-2*rd); sort desc. Use the standard
        # 95% lower bound (2*rd) consistent with the rest of the codebase, and
        # read the real Glicko2Player fields (r, not a non-existent "rating").
        from glicko2 import Glicko2Player
        scored = []
        for name in active:
            if f"claude_v{source_v}" == name:
                continue
            try:
                rec = ratings.get(name)
                if rec is None:
                    cons = Glicko2Player().conservative_rating()  # default 1500-700=800
                elif isinstance(rec, dict):
                    cons = rec.get("r", Glicko2Player().r) - 2 * rec.get("rd", Glicko2Player().rd)
                else:
                    cons = rec.conservative_rating()  # Glicko2Player method
                scored.append((cons, name))
            except Exception:
                scored.append((0.0, name))
        scored.sort(reverse=True)
        for _cons, name in scored:
            if len(paths) >= max_opponents:
                break
            try:
                v = int(str(name).replace("claude_v", ""))
            except (ValueError, TypeError):
                continue
            d = get_bot_dir(v)
            m = d / "main.py"
            if m.exists() and str(m) not in paths:
                paths.append(str(m))
        return paths[:max_opponents]
    except Exception as e:
        log.debug("QD eval opponent selection failed: %s", e)
        return []


def launch_qd_eval(bot_v: int, source_v: int, *, k: int = 3, n_games: int = 8,
                   ui=None, timeout_sec: Optional[int] = None,
                   shutdown_mgr=None) -> bool:
    """Launch a fire-and-forget QD k=3 evaluation for the just-committed bot.

    Returns True if a worker was launched, False if skipped (single-flight or
    shutting down). Called from generation_scheduler.post_generation_cleanup on
    the main event-loop thread (which passes its shutdown_mgr).

    Emits (synchronously, main thread):
      - pipeline.qd_eval_start        (canary, always before launch)
      - pipeline.qd_eval_skipped      (if single-flight or shutdown skipped)
    The worker thread emits:
      - pipeline.qd_eval_done | _failed | _cancelled  (terminal)
    """
    from system_log import log_system_event

    # Single-flight: skip if a previous QD eval is still running. Atomic because
    # post_generation_cleanup runs on one event loop (no await between is_set/set).
    if _qd_eval_running.is_set():
        log.info("QD eval skipped for v%s (previous still running)", bot_v)
        log_system_event(
            _QD_EVENT_SKIPPED, "info",
            f"v{bot_v} QD eval skipped: previous still running",
            {"version": bot_v, "reason": "single_flight_skip"},
        )
        return False

    # Respect an in-progress shutdown (but observe it — never silent).
    if shutdown_mgr is not None and getattr(shutdown_mgr, "is_shutting_down", False):
        log.info("QD eval skipped for v%s (shutting down)", bot_v)
        log_system_event(
            _QD_EVENT_SKIPPED, "info",
            f"v{bot_v} QD eval skipped: shutting down",
            {"version": bot_v, "reason": "shutdown"},
        )
        return False

    # Canary: emitted in the MAIN thread synchronously before launch.
    log_system_event(
        _QD_EVENT_START, "info",
        f"QD eval starting v{bot_v}",
        {"version": bot_v, "source_v": source_v, "k": k, "n_games": n_games,
         "started_at": time.time(), "mode": "background"},
    )

    _qd_eval_running.set()
    _qd_cancel.clear()
    global _qd_cancel_reason
    with _qd_cancel_reason_lock:
        _qd_cancel_reason = None
    _bot_v = bot_v
    _source_v = source_v
    _k = k
    _n_games = n_games
    _timeout = timeout_sec if timeout_sec is not None else ASYNC_EVAL_TIMEOUT_SEC

    def _qd_eval_worker():
        """Run k=3 mirror evaluations off the event loop, then merge into the
        behavior archive. Daemon thread; uses ONLY logging + log_system_event
        (thread-safe). Never touches ui (asyncio-Queue race from non-loop thread).
        """
        def _watchdog_fire():
            global _qd_cancel_reason
            with _qd_cancel_reason_lock:
                if not _qd_cancel.is_set():  # don't clobber an earlier "shutdown" reason
                    _qd_cancel_reason = "watchdog"
            _qd_cancel.set()
            try:
                from system_log import log_system_event
                log_system_event(
                    "pipeline.qd_eval_watchdog", "warn",
                    f"v{_bot_v} QD eval watchdog fired at {_timeout}s — result kept if usable",
                    {"version": _bot_v, "timeout_sec": _timeout},
                )
            except Exception:
                pass

        watchdog = threading.Timer(_timeout, _watchdog_fire)
        watchdog.daemon = True
        watchdog.start()
        try:
            from evolution_infra import get_bot_dir, write_locked_json
            from qd_fitness import evaluate_commit_version_k
            from map_elites import BEHAVIOR_ARCHIVE_FILE, read_behavior_archive

            new_bot_dir = get_bot_dir(_bot_v)
            new_bot_main = new_bot_dir / "main.py"
            if not new_bot_main.exists():
                log.warning("QD eval v%s: bot main.py missing", _bot_v)
                log_system_event(
                    _QD_EVENT_FAILED, "warn",
                    f"v{_bot_v} QD eval failed: bot main.py missing",
                    {"version": _bot_v, "reason": "bot_main_missing",
                     "path": str(new_bot_main)},
                )
                return

            opponents = _select_eval_opponents(_source_v, max_opponents=3)
            # Exclude the candidate itself: by the time post_generation_cleanup
            # runs, the candidate is already committed + tagged (hence in
            # get_active_bots()). _select_eval_opponents excludes the parent
            # (source_v) but not the candidate; a self mirror_battle would yield
            # ~0.5 win-rate samples and inflate the median fitness.
            try:
                from pathlib import Path
                _cand_resolved = Path(str(new_bot_main)).resolve()
                opponents = [o for o in opponents if Path(o).resolve() != _cand_resolved]
            except Exception:
                pass
            if not opponents:
                log.warning("QD eval v%s: no opponents available", _bot_v)
                log_system_event(
                    _QD_EVENT_FAILED, "warn",
                    f"v{_bot_v} QD eval failed: no opponents",
                    {"version": _bot_v, "reason": "no_opponents"},
                )
                return

            # Check cancel between setup and the (longer) eval loop.
            if _qd_cancel.is_set():
                log.info("QD eval v%s cancelled before eval", _bot_v)
                log_system_event(
                    _QD_EVENT_CANCELLED, "info",
                    f"v{_bot_v} QD eval cancelled (pre-eval)",
                    {"version": _bot_v, "reason": "cancel_pre_eval"},
                )
                return

            result = evaluate_commit_version_k(
                str(new_bot_main), opponents, k=_k, n_games=_n_games,
                cancel_check=_qd_cancel.is_set,
            )

            _cancel_reason = _qd_cancel_reason
            if _qd_cancel.is_set() and _cancel_reason != "watchdog":
                # Shutdown (or legacy unknown) cancel: discard — the process is
                # exiting, so writing the archive is pointless/unsafe.
                log.info("QD eval v%s cancelled after eval, reason=%s (result discarded)", _bot_v, _cancel_reason)
                log_system_event(
                    _QD_EVENT_CANCELLED, "info",
                    f"v{_bot_v} QD eval cancelled (post-eval; archive not updated)",
                    {"version": _bot_v, "reason": _cancel_reason or "cancel_post_eval",
                     "fitness_median": result.get("fitness_median")},
                )
                return
            if _qd_cancel.is_set() and _cancel_reason == "watchdog":
                # Watchdog fired but the eval COMPLETED with usable samples — KEEP
                # the result (root-cause fix: old code discarded it here, causing
                # 100% cancellation and zero k3 archive entries). Fall through to
                # the archive merge below.
                _has_data = bool(result and result.get("fitness_samples"))
                if not _has_data:
                    log_system_event(
                        _QD_EVENT_CANCELLED, "info",
                        f"v{_bot_v} QD eval watchdog-timed with no usable data (discarded)",
                        {"version": _bot_v, "reason": "watchdog_no_data"},
                    )
                    return
                log.info("QD eval v%s watchdog-timed post-eval — KEEPING result (samples=%s)",
                         _bot_v, result.get("fitness_samples"))
                log_system_event(
                    "pipeline.qd_eval_watchdog_kept", "info",
                    f"v{_bot_v} QD eval watchdog-timed but result kept (samples={result.get('fitness_samples')})",
                    {"version": _bot_v, "fitness_median": result.get("fitness_median"),
                     "fitness_samples": result.get("fitness_samples")},
                )
                # fall through to archive merge below

            # Merge into behavior_archive.json (atomic, best-effort).
            try:
                archive = read_behavior_archive() or {}
                niches = archive.get("niches") if isinstance(archive, dict) else None
                if isinstance(niches, dict):
                    bot_name = f"claude_v{_bot_v}"
                    updated = False
                    for key, entry in niches.items():
                        if entry.get("bot") == bot_name:
                            entry["fitness_samples"] = result["fitness_samples"]
                            entry["fitness_median"] = result["fitness_median"]
                            entry["eval_mode"] = "k3"
                            entry["last_eval"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                            updated = True
                            break
                    if updated:
                        archive["niches"] = niches
                        archive["cells"] = niches
                        bot_niches = archive.get("bot_niches")
                        if isinstance(bot_niches, dict) and isinstance(bot_niches.get(bot_name), dict):
                            bot_niches[bot_name]["fitness_median"] = result["fitness_median"]
                            bot_niches[bot_name]["fitness_samples"] = result["fitness_samples"]
                            bot_niches[bot_name]["eval_mode"] = "k3"
                            bot_niches[bot_name]["fitness"] = result["fitness_median"]
                        write_locked_json(BEHAVIOR_ARCHIVE_FILE, archive)
            except Exception as e:
                log.warning("QD eval v%s archive merge failed: %s", _bot_v, e)

            log_system_event(
                _QD_EVENT_DONE, "info",
                f"v{_bot_v} QD eval done: median {result['fitness_median']:.3f} "
                f"(samples={result['fitness_samples']}, k={result['k']})",
                {"version": _bot_v, "source_v": _source_v,
                 "fitness_median": result["fitness_median"],
                 "fitness_samples": result["fitness_samples"],
                 "completed": result["completed"], "k": result["k"],
                 "n_opponents": len(opponents)},
            )
        except Exception as e:
            log.warning(
                "QD eval failed for v%s: %s\n%s", _bot_v, e, traceback.format_exc()
            )
            log_system_event(
                _QD_EVENT_FAILED, "error",
                f"v{_bot_v} QD eval failed: {e}",
                {"version": _bot_v, "error": str(e)[:300],
                 "traceback": traceback.format_exc()[:2000]},
            )
        finally:
            try:
                watchdog.cancel()
            except Exception:
                pass
            _qd_eval_running.clear()

    threading.Thread(
        target=_qd_eval_worker, daemon=True,
        name=f"qd-eval-v{bot_v}",
    ).start()
    log.info("QD eval launched in background for v%s (k=%d)", bot_v, k)
    return True
