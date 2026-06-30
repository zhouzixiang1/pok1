"""Daemon subprocess lifecycle management.

Handles starting, stopping, monitoring, and orphan detection for the
elo_daemon.py background process.
"""

import atexit
import json
import logging
import os
import signal
import subprocess
import sys
import time
import threading

from evolution_infra import RESULTS_DIR
from system_log import log_system_event

log = logging.getLogger("pok.infra")

# Global daemon process handle
daemon_proc = None
_daemon_lock = threading.Lock()
_atexit_registered = False
_daemon_shutting_down = False


def _drain_stdout(proc):
    """Drain daemon stdout to prevent pipe buffer deadlock."""
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            log.debug("[DAEMON] %s", line.rstrip())
    except (ValueError, OSError):
        pass  # Pipe closed


# Upper bound on daemon workers. Each worker runs mirror battles, which each
# spawn two bot subprocesses, so peak RSS scales ~3x per worker. On a 32-core
# box the old default (28 workers) was repeatedly OOM-killed (rc=-9 storm,
# 2026-06-16), which took down the Battle Scheduler and stranded precommit_eval.
# 12 workers still saturates a big machine for this I/O-bound bot workload but
# keeps peak memory well under the OOM threshold.
MAX_SAFE_DAEMON_WORKERS = 12


def _default_daemon_workers() -> int:
    """Default daemon workers = CPU cores * 7/8, clamped to [1, MAX_SAFE_DAEMON_WORKERS].

    The hard cap prevents OOM-kills on high-core machines (each mirror battle
    forks two bot subprocesses, so memory scales 3x per worker)."""
    return max(1, min(MAX_SAFE_DAEMON_WORKERS, int(os.cpu_count() * 28 / 32)))


def start_daemon(workers=None, pairs=5, scheduler_capable=True):
    """Start elo_daemon.py as a background subprocess in its own process group."""
    global daemon_proc, _atexit_registered, _daemon_shutting_down
    if workers is None:
        workers = _default_daemon_workers()

    from evolution_infra import CORE_DIR, RESULTS_DIR

    with _daemon_lock:
        # Clear any stale shutdown flag from a previous stop_daemon() so the
        # new daemon (and its monitor thread) can actually run. Both stop_daemon
        # and start_daemon now mutate this flag under _daemon_lock (C4), so the
        # prior pre-lock assignment race is closed.
        _daemon_shutting_down = False
        # Check in-memory handle first — if daemon is alive, no need to touch PID file.
        # This MUST happen before reading the PID file to avoid killing a running daemon
        # whose PID file still exists from a previous start_daemon() call.
        if daemon_proc and daemon_proc.poll() is None:
            log_system_event(
                "daemon.already_running", "info",
                f"Daemon already running (pid={daemon_proc.pid})",
                {"pid": daemon_proc.pid, "workers": workers, "pairs": pairs},
            )
            return daemon_proc  # Already running

        # Daemon is dead or never started — check PID file for orphan from a previous process
        daemon_pid_file = RESULTS_DIR / ".daemon_pid"
        if daemon_pid_file.exists():
            try:
                raw = daemon_pid_file.read_text().strip()
                try:
                    info = json.loads(raw)
                    old_pid = info["pid"] if isinstance(info, dict) else int(raw)
                except (json.JSONDecodeError, KeyError, TypeError):
                    old_pid = int(raw)
                try:
                    os.killpg(os.getpgid(old_pid), signal.SIGTERM)
                    log_system_event(
                        "daemon.orphan_found", "warn",
                        f"Found stale daemon pid file; sent SIGTERM to orphan pid={old_pid}",
                        {"pid": old_pid},
                    )
                    time.sleep(0.5)  # Wait for orphan to die
                    log_system_event(
                        "daemon.orphan_killed", "info",
                        f"Stale daemon orphan cleanup finished for pid={old_pid}",
                        {"pid": old_pid},
                    )
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            except ValueError:
                pass
        daemon_pid_file.unlink(missing_ok=True)
        daemon_script = str(CORE_DIR / "elo_daemon.py")
        cmd = [sys.executable, daemon_script, "--workers", str(workers), "--pairs", str(pairs)]
        daemon_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            start_new_session=True,  # Independent process group for clean killpg
            # Tag the daemon subprocess so event_bus._detect_proc() identifies it
            # as "daemon" — its events/SSE then carry the correct process identity
            # (RC6). The daemon serves many generations, so it does NOT receive a
            # pinned run_id; its events resolve the current generation's run_id
            # from the live pipeline_state.json at emit time.
            env={**os.environ, "POK_PROC": "daemon"},
        )
        tmp_pid = daemon_pid_file.with_suffix(".tmp")
        # C3: fsync before atomic replace so a crash/power loss can't leave an
        # empty/torn PID file (which would make is_daemon_scheduler_capable()
        # hit JSONDecodeError and strand precommit jobs).
        _pid_fd = os.open(str(tmp_pid), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(_pid_fd, json.dumps({"pid": daemon_proc.pid, "ppid": os.getpid(), "scheduler_capable": scheduler_capable}).encode("utf-8"))
            os.fsync(_pid_fd)
        finally:
            os.close(_pid_fd)
        os.replace(str(tmp_pid), str(daemon_pid_file))
        log_system_event(
            "daemon.pid_written", "info",
            f"Daemon PID file written for pid={daemon_proc.pid}",
            {"pid": daemon_proc.pid, "ppid": os.getpid(),
             "workers": workers, "pairs": pairs,
             "scheduler_capable": scheduler_capable},
        )
    # Drain daemon stdout to prevent pipe buffer deadlock
    threading.Thread(target=_drain_stdout, args=(daemon_proc,), daemon=True).start()
    if not _atexit_registered:
        atexit.register(stop_daemon)
        _atexit_registered = True
    from system_log import log_system_event
    log_system_event("daemon.started", "success", f"Daemon started (workers={workers}, pairs={pairs})",
                     {"workers": workers, "pairs": pairs})
    return daemon_proc


def stop_daemon():
    """Stop the daemon subprocess and its entire process group."""
    global daemon_proc, _daemon_shutting_down
    # C4: set _daemon_shutting_down INSIDE the lock (moved from before it) so it
    # is mutated atomically with start_daemon, which clears it under the same
    # lock. The old pre-lock assignment left a window where a racing start_daemon
    # could spawn a fresh daemon that this stop then killed (code's own comment
    # at the start_daemon flag-clear acknowledged this race).
    with _daemon_lock:
        _stop_t0 = time.time()
        _daemon_shutting_down = True
        if daemon_proc is None:
            # No in-memory handle — try PID file for orphan cleanup
            log_system_event(
                "daemon.stop_requested", "info",
                "Daemon stop requested with no in-memory process handle",
                {"pid": None},
            )
            _kill_orphan_from_pid_file()
            return
        log_system_event(
            "daemon.stop_requested", "info",
            f"Daemon stop requested for pid={daemon_proc.pid}",
            {"pid": daemon_proc.pid},
        )
        if daemon_proc.poll() is None:
            try:
                pgid = os.getpgid(daemon_proc.pid)
            except (ProcessLookupError, PermissionError):
                pgid = None
            try:
                if pgid is not None:
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    daemon_proc.terminate()
            except (ProcessLookupError, PermissionError):
                daemon_proc.terminate()
            try:
                # RC3: graceful shutdown (cancel in-flight mirror battles + fcntl
                # save_cycle of ratings/h2h/stats) takes ~2-3s under load; the old
                # 3s was right at the edge, so daemon frequently hit SIGKILL (rc=-9)
                # on stop/restart — monitor then logged it as "daemon.crashed" and
                # auto-restarted (benign but noisy + wastes in-flight battles).
                # 8s gives comfortable headroom; SIGKILL below is the backstop for a
                # truly wedged daemon.
                daemon_proc.wait(timeout=8)
                rc = getattr(daemon_proc, "returncode", daemon_proc.poll())
                log_system_event(
                    "daemon.stop_result", "success",
                    f"Daemon stopped gracefully (pid={daemon_proc.pid}, rc={rc})",
                    {"pid": daemon_proc.pid, "returncode": rc,
                     "elapsed_sec": round(time.time() - _stop_t0, 2), "forced": False},
                )
            except subprocess.TimeoutExpired:
                log.warning("Daemon did not exit gracefully in 8s — force killing (SIGKILL)")
                # Group B: record force-kill so rc=-9 events can be attributed to
                # stop_daemon's 8s backstop (daemon stuck in save_cycle / heavy I/O)
                # vs an external SIGKILL / OOM killer.
                log_system_event(
                    "daemon.force_killed", "warn",
                    "stop_daemon: daemon did not exit in 8s, sent SIGKILL (rc=-9). "
                    "Likely stuck in save_cycle fcntl or heavy battle I/O.",
                    {"pid": daemon_proc.pid if daemon_proc else None},
                )
                try:
                    if pgid is not None:
                        os.killpg(pgid, signal.SIGKILL)
                    else:
                        daemon_proc.kill()
                except (ProcessLookupError, PermissionError):
                    daemon_proc.kill()
                try:
                    daemon_proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                log_system_event(
                    "daemon.stop_result", "warn",
                    f"Daemon required SIGKILL (pid={daemon_proc.pid})",
                    {"pid": daemon_proc.pid, "returncode": daemon_proc.poll(),
                     "elapsed_sec": round(time.time() - _stop_t0, 2), "forced": True},
                )
        daemon_proc = None
        # Clean up PID file
        daemon_pid_file = RESULTS_DIR / ".daemon_pid"
        daemon_pid_file.unlink(missing_ok=True)
    log_system_event("daemon.stopped", "info", "Daemon stopped")


def _kill_orphan_from_pid_file():
    """Kill any orphan daemon process recorded in the PID file."""
    daemon_pid_file = RESULTS_DIR / ".daemon_pid"
    if not daemon_pid_file.exists():
        return
    try:
        raw = daemon_pid_file.read_text().strip()
        try:
            info = json.loads(raw)
            old_pid = info["pid"] if isinstance(info, dict) else int(raw)
        except (json.JSONDecodeError, KeyError, TypeError):
            old_pid = int(raw)
        try:
            pgid = os.getpgid(old_pid)
            os.killpg(pgid, signal.SIGTERM)
            time.sleep(0.5)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        except (ProcessLookupError, PermissionError, OSError):
            pass
    except (ValueError, OSError):
        pass
    daemon_pid_file.unlink(missing_ok=True)


def is_daemon_alive():
    """Check if daemon subprocess is running."""
    with _daemon_lock:
        proc = daemon_proc
    return proc is not None and proc.poll() is None


def is_daemon_scheduler_capable():
    """Check if the running daemon is alive, was started with scheduler capability,
    AND its main loop is recently active.

    The liveness check (is_daemon_alive) is essential: the .daemon_pid file
    outlives an OOM-killed daemon (rc=-9 storm, 2026-06-16), and without it the
    stale scheduler_capable=true flag convinced precommit_eval the scheduler
    was usable — jobs were submitted to a dead daemon and never completed,
    stranding v107's precommit forever. A capability flag on a dead process
    is meaningless.

    v193 root-cause-audit (2026-06-26): the static `scheduler_capable` flag alone
    could NOT detect a HALF-DEAD daemon — process alive (poll()==None) but main
    loop stalled (not draining jobs). v193's precommit jobs were submitted to such
    a half-dead daemon and never executed, stranding the whole generation for 70min
    until CYCLE_TIMEOUT. The daemon now writes a `last_heartbeat` into .daemon_pid
    each main-loop iteration; require it to be fresher than HEARTBEAT_STALE_SEC so
    a stalled loop (regardless of cause) is treated as incapable and precommit
    falls back to the parallel path instead of waiting on a dead scheduler.
    """
    if not is_daemon_alive():
        return False
    from evolution_infra import RESULTS_DIR
    daemon_pid_file = RESULTS_DIR / ".daemon_pid"
    if not daemon_pid_file.exists():
        return False
    try:
        raw = daemon_pid_file.read_text().strip()
        if raw.isdigit():
            return False
        info = json.loads(raw)
        if not info.get("scheduler_capable", False):
            return False
        # Heartbeat freshness: a daemon whose main loop has stalled (but process
        # still alive) must not be reported as capable. Tolerate a missing
        # heartbeat for older daemons that predate the heartbeat field, but once
        # a heartbeat exists, enforce freshness.
        hb = info.get("last_heartbeat")
        if hb is not None:
            try:
                from elo_daemon import HEARTBEAT_STALE_SEC
            except Exception:
                HEARTBEAT_STALE_SEC = 120
            try:
                if time.time() - float(hb) > HEARTBEAT_STALE_SEC:
                    return False
            except (TypeError, ValueError):
                return False
        return True
    except (json.JSONDecodeError, KeyError, TypeError, OSError):
        return False


def daemon_monitor_thread(ui, stop_event, daemon_workers=None, daemon_pairs=5):
    """Background thread: reads daemon stats, updates UI, auto-restarts dead daemon."""
    global daemon_proc  # written below (daemon_proc = None); must be declared global
    if not ui:
        return
    if daemon_workers is None:
        daemon_workers = _default_daemon_workers()
    from evolution_infra import load_daemon_stats, load_ratings
    restart_count = 0
    while not stop_event.is_set():
        # Check shutdown flag first to prevent restart race
        if _daemon_shutting_down:
            break
        try:
            with _daemon_lock:
                proc = daemon_proc
            if proc is not None and proc.poll() is not None:
                rc = proc.poll()
                # Re-check under lock — start_daemon may have replaced daemon_proc
                with _daemon_lock:
                    current_proc = daemon_proc
                # Determine if this was a crash-recovery restart or intentional stop
                if current_proc is not None and current_proc is not proc and current_proc.poll() is None:
                    # Daemon was replaced by another actor (web UI, orchestrator, etc.)
                    # Don't count against this monitor's restart budget — it wasn't our restart.
                    restart_count = 0
                elif rc == 0:
                    # v193 root-cause-audit (2026-06-26): a clean rc=0 exit is NOT a
                    # crash. With the keep-alive main loop, the daemon now exits rc=0
                    # when it has been idle (no in_flight / external jobs) for
                    # DAEMON_IDLE_MAX_SEC, OR when orphaned (parent died), OR on a
                    # graceful SIGTERM. None of these consume the crash-restart
                    # budget — treat them like an intentional stop so repeated
                    # idle-exit→restart cycles don't trip the "failed 5x" guard and
                    # permanently stop auto-restart. Only non-zero rc (OOM/SIGKILL/
                    # BrokenProcessPool) counts as a crash.
                    restart_count = 0
                    with _daemon_lock:
                        if daemon_proc is proc:
                            daemon_proc = None
                    log_system_event(
                        "daemon.exited_cleanly", "info",
                        f"Daemon exited cleanly (rc=0, pid={proc.pid})",
                        {"pid": proc.pid, "returncode": rc},
                    )
                else:
                    restart_count += 1
                    # Clear stale handle immediately so other callers see the
                    # daemon as dead during the backoff sleep window.
                    with _daemon_lock:
                        if daemon_proc is proc:
                            daemon_proc = None

                if restart_count > 5:
                    ui.log_history(f"Daemon failed 5x consecutively, stopping auto-restart (last rc={rc})", "error")
                    from system_log import log_system_event
                    log_system_event("daemon.crashed", "error", f"Daemon failed {restart_count}x, auto-restart stopped",
                                     {"restart_count": restart_count, "returncode": rc})
                    break
                if restart_count > 0:
                    backoff = min(3 * (2 ** (restart_count - 1)), 120)
                    ui.log_history(f"⚠️ Daemon exited (rc={rc}), restarting in {backoff}s (attempt {restart_count})", "warn")
                    from system_log import log_system_event
                    log_system_event("daemon.crashed", "error", f"Daemon exited rc={rc}, restarting (attempt {restart_count})",
                                     {"restart_count": restart_count, "returncode": rc})
                    if stop_event.wait(backoff):
                        break
                    if _daemon_shutting_down:
                        break
                    # restart 时实际检测 battle_scheduler 可用性,而非读
                    # is_daemon_scheduler_capable()——该函数第一步 is_daemon_alive()
                    # 在 restart 上下文里 daemon 已死 → 必返回 False,导致
                    # scheduler_capable 被 stale 值锁死 false(daemon crash 一次后
                    # 永久 false,precommit 永走慢路径 parallel mirror battle)。
                    try:
                        import battle_scheduler  # noqa: F401 — 实际 import 测试可用性
                        _restart_scheduler_capable = True
                    except Exception:
                        _restart_scheduler_capable = False
                    start_daemon(workers=daemon_workers, pairs=daemon_pairs, scheduler_capable=_restart_scheduler_capable)
            else:
                restart_count = 0
            stats = load_daemon_stats()
            ratings = load_ratings()
            ui.update_daemon_status(stats, ratings)
        except Exception as e:
            ui.log_history(f"Daemon monitor error: {e}", "error")
            try:
                log_system_event(
                    "daemon.monitor_error", "error",
                    f"Daemon monitor error: {e}",
                    {"error": str(e)},
                )
            except Exception:
                pass
        stop_event.wait(3)
