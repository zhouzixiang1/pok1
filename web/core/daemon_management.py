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
from system_log import log_system_event as _persist_system_event

log = logging.getLogger("pok.infra")


def log_system_event(event_type, severity, message, data=None):
    """Persist daemon lifecycle events only inside an initialized epoch.

    Stopping a stale process remains allowed before reset, but that safety path
    must not append lifecycle rows to the retired event ledger.  Existing tests
    and callers can still monkeypatch this module-local compatibility name.
    """

    try:
        from epoch_authority import require_policy_epoch_initialized

        require_policy_epoch_initialized("daemon_management.event")
    except Exception:
        return
    _persist_system_event(event_type, severity, message, data)

# Global daemon process handle
daemon_proc = None
_daemon_lock = threading.Lock()
_atexit_registered = False
_daemon_shutting_down = False


def _daemon_exit_metadata(returncode):
    """Classify daemon subprocess exit for structured monitoring logs."""
    if returncode is None:
        return {"exit_cause": "unknown", "signal": None, "killer_known": False}
    if returncode < 0:
        signum = abs(int(returncode))
        try:
            sig_name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            sig_name = f"SIG{signum}"
        return {"exit_cause": "signal", "signal": sig_name, "killer_known": False}
    if returncode == 0:
        return {"exit_cause": "clean", "signal": None, "killer_known": True}
    return {"exit_cause": "process_error", "signal": None, "killer_known": False}


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


# Upper bound on daemon workers. Each worker runs one complete 70-hand native
# TCP match with two managed bot subprocesses, so peak RSS scales with worker
# count. Twelve workers use the machine without recreating the old OOM storm.
MAX_SAFE_DAEMON_WORKERS = 12


def _default_daemon_workers() -> int:
    """Default daemon workers = CPU cores * 7/8, clamped to [1, MAX_SAFE_DAEMON_WORKERS].

    The hard cap prevents OOM-kills on high-core machines."""
    return max(1, min(MAX_SAFE_DAEMON_WORKERS, int(os.cpu_count() * 28 / 32)))


def start_daemon(workers=None, pairs=5):
    """Start elo_daemon.py as a background subprocess in its own process group."""
    global daemon_proc, _atexit_registered, _daemon_shutting_down
    # This check must precede the daemon lock, stale-PID cleanup, unlink, Popen,
    # and event emission.  Until the one-time reset is valid, all of those
    # would mutate or reconstruct the retired rating epoch.
    from epoch_authority import require_policy_epoch_initialized

    require_policy_epoch_initialized("daemon_management.start_daemon")
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
        # Fsync before atomic replace so a crash/power loss cannot leave a
        # torn process-health record.
        _pid_fd = os.open(str(tmp_pid), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(
                _pid_fd,
                json.dumps({"pid": daemon_proc.pid, "ppid": os.getpid()}).encode(
                    "utf-8"
                ),
            )
            os.fsync(_pid_fd)
        finally:
            os.close(_pid_fd)
        os.replace(str(tmp_pid), str(daemon_pid_file))
        log_system_event(
            "daemon.pid_written", "info",
            f"Daemon PID file written for pid={daemon_proc.pid}",
            {"pid": daemon_proc.pid, "ppid": os.getpid(),
             "workers": workers, "pairs": pairs},
        )
    # Drain daemon stdout to prevent pipe buffer deadlock
    threading.Thread(target=_drain_stdout, args=(daemon_proc,), daemon=True).start()
    if not _atexit_registered:
        atexit.register(stop_daemon)
        _atexit_registered = True
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
                # Graceful shutdown (cancel in-flight native matches + fcntl
                # save_cycle of ratings/H2H/stats) takes ~2-3s under load; the old
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


def daemon_monitor_thread(ui, stop_event, daemon_workers=None, daemon_pairs=5):
    """Background thread: reads daemon stats, updates UI, auto-restarts dead daemon."""
    global daemon_proc  # written below (daemon_proc = None); must be declared global
    try:
        from epoch_authority import require_policy_epoch_initialized

        require_policy_epoch_initialized("daemon_management.monitor")
    except Exception as exc:
        if ui:
            state = getattr(exc, "state", {})
            ui.log_history(
                "Daemon monitor not started: policy epoch initialization is "
                f"{state.get('state', 'unavailable')}",
                "warn",
            )
        return
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
                    shutting_down = _daemon_shutting_down
                # Determine if this was a crash-recovery restart or intentional stop
                if current_proc is not None and current_proc is not proc and current_proc.poll() is None:
                    # Daemon was replaced by another actor (web UI, orchestrator, etc.)
                    # Don't count against this monitor's restart budget — it wasn't our restart.
                    restart_count = 0
                elif shutting_down:
                    # stop_daemon() owns this exit. It may have had to send the
                    # SIGKILL backstop after graceful SIGTERM; that is a forced
                    # stop, not an unexpected daemon crash or auto-restart signal.
                    restart_count = 0
                    with _daemon_lock:
                        if daemon_proc is proc:
                            daemon_proc = None
                    severity = "warn" if rc not in (0, None) else "info"
                    log_system_event(
                        "daemon.exited_after_stop",
                        severity,
                        f"Daemon exited after stop request (rc={rc}, pid={proc.pid})",
                        {"pid": proc.pid, "returncode": rc, "forced": rc == -signal.SIGKILL},
                    )
                    break
                elif rc == 0:
                    # A clean exit is not a crash. Orphan detection and graceful
                    # shutdown both use rc=0; only non-zero exits consume the
                    # bounded crash-restart budget.
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
                    exit_meta = _daemon_exit_metadata(rc)
                    log_system_event("daemon.crashed", "error", f"Daemon failed {restart_count}x, auto-restart stopped",
                                     {"restart_count": restart_count, "returncode": rc, **exit_meta})
                    break
                if restart_count > 0:
                    backoff = min(3 * (2 ** (restart_count - 1)), 120)
                    ui.log_history(f"⚠️ Daemon exited (rc={rc}), restarting in {backoff}s (attempt {restart_count})", "warn")
                    exit_meta = _daemon_exit_metadata(rc)
                    log_system_event("daemon.crashed", "error", f"Daemon exited rc={rc}, restarting (attempt {restart_count})",
                                     {"restart_count": restart_count, "returncode": rc, **exit_meta})
                    if stop_event.wait(backoff):
                        break
                    if _daemon_shutting_down:
                        break
                    start_daemon(workers=daemon_workers, pairs=daemon_pairs)
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
