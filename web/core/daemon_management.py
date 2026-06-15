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


def _default_daemon_workers() -> int:
    """Default daemon workers = CPU cores * 7/8, clamped to [1, 128]."""
    return max(1, int(os.cpu_count() * 28 / 32))


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
    """Check if the running daemon was started with scheduler capability."""
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
                    # Preserve original scheduler_capable flag on restart
                    was_scheduler_capable = is_daemon_scheduler_capable()
                    start_daemon(workers=daemon_workers, pairs=daemon_pairs, scheduler_capable=was_scheduler_capable)
            else:
                restart_count = 0
            stats = load_daemon_stats()
            ratings = load_ratings()
            ui.update_daemon_status(stats, ratings)
        except Exception as e:
            ui.log_history(f"Daemon monitor error: {e}", "error")
        stop_event.wait(3)
