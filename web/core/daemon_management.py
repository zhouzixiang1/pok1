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
        # new daemon (and its monitor thread) can actually run. stop_daemon()
        # sets the flag before acquiring the lock (line 106), so a narrow
        # race window exists, but it is pre-existing and extremely unlikely.
        _daemon_shutting_down = False
        # Check in-memory handle first — if daemon is alive, no need to touch PID file.
        # This MUST happen before reading the PID file to avoid killing a running daemon
        # whose PID file still exists from a previous start_daemon() call.
        if daemon_proc and daemon_proc.poll() is None:
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
                    time.sleep(0.5)  # Wait for orphan to die
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
        tmp_pid.write_text(json.dumps({"pid": daemon_proc.pid, "ppid": os.getpid(), "scheduler_capable": scheduler_capable}))
        os.replace(str(tmp_pid), str(daemon_pid_file))
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
    _daemon_shutting_down = True
    with _daemon_lock:
        if daemon_proc is None:
            # No in-memory handle — try PID file for orphan cleanup
            _kill_orphan_from_pid_file()
            return
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
    """Check if the running daemon is alive AND was started with scheduler capability.

    The liveness check (is_daemon_alive) is essential: the .daemon_pid file
    outlives an OOM-killed daemon (rc=-9 storm, 2026-06-16), and without it the
    stale scheduler_capable=true flag convinced precommit_eval the scheduler
    was usable — jobs were submitted to a dead daemon and never completed,
    stranding v107's precommit forever. A capability flag on a dead process
    is meaningless.
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
        return info.get("scheduler_capable", False)
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
        stop_event.wait(3)
