"""Daemon subprocess lifecycle management.

Handles starting, stopping, monitoring, and orphan detection for the
elo_daemon.py background process.
"""

import atexit
import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import secrets
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
_DAEMON_OWNER_TOKEN_ENV = "POK_DAEMON_OWNER_TOKEN"
_DAEMON_GRACEFUL_ORPHAN_TIMEOUT_SEC = 8.0
_DAEMON_FORCE_ORPHAN_TIMEOUT_SEC = 2.0

# The monitor loop used to poll every 3 seconds, and each tick rebuilt the
# strict-evaluation read projection consumed by ``ui.update_daemon_status``:
# that rebuild re-reads the immutable evaluation cycle and SHA256-verifies the
# raw replay corpus it references (hundreds of MB), which dominated the web
# process CPU.  Liveness (auto-restart / stability reset) only needs bounded
# detection latency, so the tick interval is operator tunable and defaults to
# 30 seconds; the expensive projection itself is additionally fingerprint
# gated below.
DAEMON_MONITOR_INTERVAL_ENV = "POK_DAEMON_MONITOR_INTERVAL_SEC"
DAEMON_MONITOR_INTERVAL_DEFAULT_SEC = 30.0
DAEMON_MONITOR_INTERVAL_MIN_SEC = 3.0
DAEMON_MONITOR_INTERVAL_MAX_SEC = 600.0


def daemon_monitor_interval_from_env() -> float:
    """Parse ``POK_DAEMON_MONITOR_INTERVAL_SEC``, clamped to [3, 600] seconds."""
    try:
        value = float(
            os.environ.get(
                DAEMON_MONITOR_INTERVAL_ENV,
                DAEMON_MONITOR_INTERVAL_DEFAULT_SEC,
            )
        )
    except (TypeError, ValueError):
        return DAEMON_MONITOR_INTERVAL_DEFAULT_SEC
    if not math.isfinite(value):
        return DAEMON_MONITOR_INTERVAL_DEFAULT_SEC
    return max(
        DAEMON_MONITOR_INTERVAL_MIN_SEC,
        min(DAEMON_MONITOR_INTERVAL_MAX_SEC, value),
    )


DAEMON_MONITOR_INTERVAL_SEC = daemon_monitor_interval_from_env()


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
# One daemon pair is one complete 70-hand native match.  The evaluation
# protocol caps this per-pairing sample budget at eight; it is not a strength
# verdict and must not be silently represented as a larger effective value.
MAX_DAEMON_PAIRS = 8


def default_daemon_workers() -> int:
    """Default daemon workers = CPU cores * 7/8, clamped to [1, MAX_SAFE_DAEMON_WORKERS].

    The hard cap prevents OOM-kills on high-core machines."""
    return max(
        1,
        min(MAX_SAFE_DAEMON_WORKERS, int((os.cpu_count() or 1) * 28 / 32)),
    )


def _proc_identity(pid: int) -> tuple[int, str] | None:
    """Return Linux start ticks and process state from one proc snapshot."""

    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as handle:
            fields = handle.read().split()
        value = int(fields[21])
    except (OSError, ValueError, IndexError, TypeError):
        return None
    return (value, str(fields[2])) if value > 0 else None


def _proc_start_ticks(pid: int) -> int | None:
    """Return Linux process start ticks, which disambiguate PID reuse."""

    identity = _proc_identity(pid)
    return identity[0] if identity is not None else None


def _read_daemon_pid_record(path) -> dict:
    raw = path.read_text(encoding="utf-8").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = int(raw)
    if isinstance(parsed, dict):
        pid = int(parsed.get("pid") or 0)
        start_ticks = int(parsed.get("start_ticks") or 0)
        return {**parsed, "pid": pid, "start_ticks": start_ticks}
    return {"pid": int(parsed), "start_ticks": 0, "legacy": True}


def _daemon_owner_contract_identity(pid: int, record: dict) -> str:
    """Prove that an exact live PID is the daemon process we launched.

    PID/start ticks prevent reuse, but a forged record could still name an
    unrelated live process and make cleanup signal its whole process group.
    Bind cleanup to a per-launch token, the exact elo_daemon.py command and the
    start-new-session group-leader invariant before any signal is sent.
    """

    expected_digest = str(record.get("owner_token_digest") or "")
    if (
        len(expected_digest) != 64
        or any(char not in "0123456789abcdef" for char in expected_digest)
    ):
        return "unverifiable"
    try:
        environ = (Path("/proc") / str(pid) / "environ").read_bytes()
        prefix = f"{_DAEMON_OWNER_TOKEN_ENV}=".encode("ascii")
        token = next(
            item[len(prefix):]
            for item in environ.split(b"\0")
            if item.startswith(prefix)
        )
        actual_digest = hashlib.sha256(token).hexdigest()
    except (OSError, StopIteration):
        return "unavailable"
    if not hmac.compare_digest(actual_digest, expected_digest):
        return "owner_mismatch"
    try:
        argv = [
            os.fsdecode(item)
            for item in (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
            if item
        ]
        expected_python = Path(sys.executable).resolve()
        expected_script = Path(__file__).resolve().with_name("elo_daemon.py")
        command_matches = (
            len(argv) == 6
            and Path(argv[0]).resolve() == expected_python
            and Path(argv[1]).resolve() == expected_script
            and argv[2] == "--workers"
            and argv[4] == "--pairs"
        )
        if command_matches:
            try:
                command_matches = (
                    1 <= int(argv[3]) <= MAX_SAFE_DAEMON_WORKERS
                    and 1 <= int(argv[5]) <= MAX_DAEMON_PAIRS
                )
            except (TypeError, ValueError, OverflowError):
                command_matches = False
    except (OSError, RuntimeError, ValueError):
        return "unavailable"
    if not command_matches:
        return "command_mismatch"
    try:
        if os.getpgid(pid) != pid:
            return "group_mismatch"
    except (ProcessLookupError, PermissionError, OSError):
        return "unavailable"
    return "match"


def _pid_record_identity(record: dict) -> str:
    """Classify a PID record without ever killing a reused/unknown PID."""

    pid = int(record.get("pid") or 0)
    if pid <= 1:
        return "invalid"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        pass
    except OSError:
        return "dead"
    proc_identity = _proc_identity(pid)
    expected = int(record.get("start_ticks") or 0)
    if proc_identity is None:
        return "unavailable"
    actual, process_state = proc_identity
    if process_state in {"Z", "X"}:
        return "dead"
    if expected <= 0:
        return "unverifiable"
    if actual != expected:
        return "reused"
    return _daemon_owner_contract_identity(pid, record)


def daemon_pid_record_health(path: str | os.PathLike[str]) -> dict:
    """Return a read-only, fail-closed daemon process identity projection.

    This is the single health boundary shared by lifecycle cleanup and the
    control plane.  A fresh heartbeat is never process authority: the exact
    PID/start-tick identity must also prove the per-launch owner token, the
    canonical daemon argv, a process-group leader, and a non-zombie proc
    state through :func:`_pid_record_identity`.

    The function never unlinks the record and never signals a process.
    """

    record_path = Path(path)
    if not os.path.lexists(record_path):
        return {
            "exists": False,
            "pid": None,
            "alive": False,
            "process_identity": "missing",
            "health_error": "daemon_pid_file_missing",
        }
    if record_path.is_symlink():
        return {
            "exists": True,
            "pid": None,
            "alive": False,
            "process_identity": "invalid",
            "health_error": "daemon_pid_record_symlink",
        }
    try:
        record = _read_daemon_pid_record(record_path)
    except (OSError, UnicodeError) as exc:
        return {
            "exists": True,
            "pid": None,
            "alive": False,
            "process_identity": "unavailable",
            "health_error": (
                f"daemon_pid_record_read_error:{type(exc).__name__}"
            ),
        }
    except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
        return {
            "exists": True,
            "pid": None,
            "alive": False,
            "process_identity": "invalid",
            "health_error": "daemon_pid_record_invalid",
        }

    data = {**record, "exists": True}
    try:
        identity = _pid_record_identity(record)
    except (TypeError, ValueError, OverflowError):
        identity = "invalid"
    data["process_identity"] = identity
    data["alive"] = identity == "match"
    if identity == "match":
        data["health_error"] = None
        return data

    if identity == "unverifiable" and int(record.get("start_ticks") or 0) <= 0:
        health_error = "daemon_start_identity_missing"
    else:
        health_error = {
            "invalid": "daemon_pid_record_invalid",
            "dead": "daemon_process_not_alive",
            "reused": "daemon_pid_reused",
            "unavailable": "daemon_process_identity_unavailable",
            "unverifiable": "daemon_owner_identity_unverifiable",
            "owner_mismatch": "daemon_owner_token_mismatch",
            "command_mismatch": "daemon_command_mismatch",
            "group_mismatch": "daemon_process_group_mismatch",
        }.get(identity, f"daemon_process_identity_{identity}")
    data["health_error"] = health_error
    return data


def _wait_for_daemon_record_exit(record: dict, timeout: float) -> bool:
    """Wait until the exact recorded daemon PID is proven gone/reused."""

    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        identity = _pid_record_identity(record)
        if identity in {"dead", "reused"}:
            return True
        if identity != "match":
            raise RuntimeError(
                f"daemon_pid_identity_{identity}:pid={record.get('pid')}"
            )
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _terminate_verified_daemon_record(record: dict) -> bool:
    """Terminate one proven daemon; return whether SIGKILL was required."""

    pid = int(record.get("pid") or 0)
    identity = _pid_record_identity(record)
    if identity in {"dead", "reused"}:
        return False
    if identity != "match":
        raise RuntimeError(f"daemon_pid_identity_{identity}:pid={pid}")
    pgid = os.getpgid(pid)
    if pgid != pid:
        raise RuntimeError(f"daemon_pid_identity_group_mismatch:pid={pid}")
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    if _wait_for_daemon_record_exit(
        record,
        _DAEMON_GRACEFUL_ORPHAN_TIMEOUT_SEC,
    ):
        return False

    # Re-open every identity proof after the grace window.  Never send the
    # force signal using only the earlier PID/PGID observation.
    if _pid_record_identity(record) != "match":
        if _wait_for_daemon_record_exit(record, 0.0):
            return False
        raise RuntimeError(f"daemon_pid_identity_changed_before_kill:pid={pid}")
    if os.getpgid(pid) != pid:
        raise RuntimeError(f"daemon_pid_identity_group_mismatch:pid={pid}")
    os.killpg(pid, signal.SIGKILL)
    if not _wait_for_daemon_record_exit(
        record,
        _DAEMON_FORCE_ORPHAN_TIMEOUT_SEC,
    ):
        raise RuntimeError(f"daemon_orphan_did_not_exit:pid={pid}")
    return True


def start_daemon(workers=None, pairs=5):
    """Start elo_daemon.py as a background subprocess in its own process group."""
    global daemon_proc, _atexit_registered, _daemon_shutting_down
    # This check must precede the daemon lock, stale-PID cleanup, unlink, Popen,
    # and event emission.  Until the one-time reset is valid, all of those
    # would mutate or reconstruct the retired rating epoch.
    from epoch_authority import require_policy_epoch_initialized

    require_policy_epoch_initialized("daemon_management.start_daemon")
    if workers is None:
        workers = default_daemon_workers()
    if (
        not isinstance(pairs, int)
        or isinstance(pairs, bool)
        or not 1 <= pairs <= MAX_DAEMON_PAIRS
    ):
        raise ValueError(
            f"daemon pairs must be an integer in [1, {MAX_DAEMON_PAIRS}]"
        )

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
                record = _read_daemon_pid_record(daemon_pid_file)
                old_pid = int(record["pid"])
                identity = _pid_record_identity(record)
                if identity == "match":
                    log_system_event(
                        "daemon.orphan_found", "warn",
                        f"Found verified daemon orphan pid={old_pid}; stopping it before replacement",
                        {"pid": old_pid},
                    )
                    forced = _terminate_verified_daemon_record(record)
                    log_system_event(
                        "daemon.orphan_killed", "info",
                        f"Stale daemon orphan cleanup finished for pid={old_pid}",
                        {"pid": old_pid, "forced": forced},
                    )
                elif identity in {"dead", "reused"}:
                    log_system_event(
                        "daemon.stale_pid_record", "warn",
                        f"Ignored stale daemon PID record ({identity})",
                        {"pid": old_pid, "identity": identity},
                    )
                else:
                    raise RuntimeError(
                        f"daemon_pid_identity_{identity}:pid={old_pid}"
                    )
            except ProcessLookupError:
                pass
            except (PermissionError, OSError) as exc:
                raise RuntimeError(
                    f"daemon_orphan_cleanup_failed:{type(exc).__name__}"
                ) from exc
        daemon_pid_file.unlink(missing_ok=True)
        daemon_script = str(CORE_DIR / "elo_daemon.py")
        cmd = [sys.executable, daemon_script, "--workers", str(workers), "--pairs", str(pairs)]
        owner_token = secrets.token_hex(32)
        owner_token_digest = hashlib.sha256(
            owner_token.encode("ascii")
        ).hexdigest()
        daemon_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            start_new_session=True,  # Independent process group for clean killpg
            # Tag the daemon subprocess so event_bus._detect_proc() identifies it
            # as "daemon" — its events/SSE then carry the correct process identity
            # (RC6). The daemon serves many generations, so it does NOT receive a
            # pinned run_id; its events resolve the current generation's run_id
            # from the live pipeline_state.json at emit time.
            env={
                **os.environ,
                "POK_PROC": "daemon",
                _DAEMON_OWNER_TOKEN_ENV: owner_token,
            },
        )
        start_ticks = _proc_start_ticks(daemon_proc.pid)
        if start_ticks is None:
            daemon_proc.terminate()
            daemon_proc.wait(timeout=2)
            daemon_proc = None
            raise RuntimeError("daemon_process_start_identity_unavailable")
        tmp_pid = daemon_pid_file.with_suffix(".tmp")
        # Fsync before atomic replace so a crash/power loss cannot leave a
        # torn process-health record.
        _pid_fd = os.open(str(tmp_pid), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(
                _pid_fd,
                json.dumps({
                    "pid": daemon_proc.pid,
                    "ppid": os.getpid(),
                    "start_ticks": start_ticks,
                    "owner_token_digest": owner_token_digest,
                }).encode(
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
             "start_ticks": start_ticks,
             "owner_token_digest": owner_token_digest,
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
        record = _read_daemon_pid_record(daemon_pid_file)
        identity = _pid_record_identity(record)
        if identity == "match":
            _terminate_verified_daemon_record(record)
        elif identity not in {"dead", "reused"}:
            raise RuntimeError(
                f"daemon_pid_identity_{identity}:pid={record.get('pid')}"
            )
    except ValueError as exc:
        raise RuntimeError("daemon_pid_record_invalid") from exc
    except OSError as exc:
        raise RuntimeError(
            f"daemon_orphan_cleanup_failed:{type(exc).__name__}"
        ) from exc
    daemon_pid_file.unlink(missing_ok=True)


def is_daemon_alive():
    """Check if daemon subprocess is running."""
    with _daemon_lock:
        proc = daemon_proc
    return proc is not None and proc.poll() is None


# ── Monitor-side strict-evaluation read projection ──────────────────────────
#
# ``ui.update_daemon_status`` projects the published strict evaluation bundle
# into the dashboard.  Building that bundle re-reads the immutable committed
# cycle and SHA256-verifies every raw match replay it references (hundreds of
# MB), so the monitor thread gates the rebuild on a cheap filesystem
# fingerprint of the exact input files the load consumes:
#
#   - policy_epoch_reset_receipt.json   (epoch reset receipt)
#   - evaluation_data_manifest.json     (evaluation identity manifest)
#   - evaluation_cycle_manifest.json    (cycle commit pointer, written last)
#   - the epoch-reset archive claim/receipt files and archived destinations
#     that the reset receipt is cross-bound to
#   - the SEMANTIC_PATHS source files hashed into the evaluation identity
#   - evaluation_cycles/**              (immutable cycle payloads + append logs)
#   - match_replay/**                   (raw replays bound by SHA256)
#   - the published active pool (git-tag derived discovery, TTL cached)
#
# This gates ONLY the read projection.  The rating daemon's own save_cycle
# full re-verification and every non-monitor ``load_current_strict_evaluation_bundle``
# caller (routes, tool status, stability observation, eval table) keep their
# direct, uncached path.
#
# The fingerprint is sampled before the load, so a file that changes between
# sampling and loading can only cause one extra rebuild on a later tick — a
# cache hit always reproduces the exact input state of the cached build, and
# a stale ``available`` bundle can never outlive a change to any consumed
# input (a git-synced source file, a retagged pool, or a tampered reset
# archive included).  Only the most recent SUCCESSFUL build is cached, and
# success means ``available: True``: the loader reports nearly all failures
# as reason dicts instead of raising (``active_pool_unavailable`` and
# friends), and caching those would freeze the dashboard on "unavailable"
# until the next evidence commit instead of retrying on the next tick.  A
# persistently failing build therefore re-runs at the tick cadence — still
# far below the old unconditional 3-second rebuild.  Inputs that are constant
# for the life of the web process (workflow profile and bot_namespace
# constants, resolved from imported code and the process environment) are
# deliberately not fingerprinted.

_DAEMON_MONITOR_BUNDLE_LOCK = threading.Lock()
_DAEMON_MONITOR_BUNDLE_CACHE = {
    "root": None,
    "fingerprint": None,
    "bundle": None,
    "valid": False,
}


def reset_monitor_strict_bundle_cache():
    """Drop the cached monitor projection (tests and operator tooling).

    Production invalidation is fingerprint-driven: an epoch reset rewrites
    the fingerprinted reset receipt, so no lifecycle code needs to call this.
    """
    with _DAEMON_MONITOR_BUNDLE_LOCK:
        _DAEMON_MONITOR_BUNDLE_CACHE["root"] = None
        _DAEMON_MONITOR_BUNDLE_CACHE["fingerprint"] = None
        _DAEMON_MONITOR_BUNDLE_CACHE["bundle"] = None
        _DAEMON_MONITOR_BUNDLE_CACHE["valid"] = False


def _fingerprint_stat(path):
    """Return the (st_mtime_ns, st_size) identity of one file, or None."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _fingerprint_file(path):
    """Return the (stat identity, sha256) of one small control file.

    The content digest binds the file even if a writer ever landed same-size
    bytes on the same mtime_ns; the cost is one small read per tick.
    """
    stat = _fingerprint_stat(path)
    if stat is None:
        return None
    try:
        return (stat, hashlib.sha256(path.read_bytes()).hexdigest())
    except OSError:
        return None


def _fingerprint_tree(path):
    """Deterministic (relative name, mtime_ns, size) scan of a directory tree.

    Hidden entries are pruned: the strict load chain never consumes them —
    replay ids are grammar-validated non-hidden top-level names and cycle
    directories are regex-validated — while the daemon stages each completed
    match under ``match_replay/.pending/`` and publishes cycles through a
    transient ``evaluation_cycles/.cycle-*`` temp directory between commits.
    Counting that staging churn would defeat the cache during exactly the
    active-daemon periods it exists for.  Like the stat-only tree identity,
    note its residual limit: a deliberate same-size, same-mtime_ns in-place
    rewrite of a large evidence file stays invisible here; save_cycle's own
    full re-verification and every direct loader call remain the authority.
    """
    entries = []
    if path.is_dir():
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = sorted(
                name for name in dirnames if not name.startswith(".")
            )
            for filename in filenames:
                if filename.startswith("."):
                    continue
                file_path = Path(dirpath) / filename
                stat = _fingerprint_stat(file_path)
                if stat is not None:
                    entries.append(
                        (file_path.relative_to(path).as_posix(), stat[0], stat[1])
                    )
    return tuple(sorted(entries))


def _monitor_semantic_fingerprint():
    """Stat identity of the source files hashed into the evaluation identity.

    ``base_evaluation_identity`` SHA256s these files on every load, so a git
    sync that updates any of them flips bundle validity
    (``cycle_manifest_evaluation_identity_invalid``) without touching the
    results root; the projection must notice that too.
    """
    try:
        from evaluation_data_identity import ROOT, SEMANTIC_PATHS
    except Exception:
        return None
    return tuple(
        _fingerprint_stat(ROOT / relative) for relative in SEMANTIC_PATHS
    )


def _monitor_epoch_archive_fingerprint(root):
    """Identity of the epoch-reset archive evidence the receipt cross-binds.

    Mirrors the project-root resolution of
    ``system_strict_bootstrap.load_policy_epoch_reset_receipt``.  When the
    receipt itself is unreadable the loader fails closed on its own, so a
    stable ``None`` here is safe.
    """
    try:
        receipt = json.loads(
            (root / "policy_epoch_reset_receipt.json").read_text(encoding="utf-8")
        )
        if not isinstance(receipt, dict):
            return None
        from system_strict_bootstrap import (
            POLICY_EPOCH_RESET_ARCHIVE_RECEIPT_FILENAME,
            POLICY_EPOCH_RESET_CLAIM_FILENAME,
        )

        project_root = (
            root.parents[2]
            if root.name == "results"
            and root.parent.name == "core"
            and root.parent.parent.name == "web"
            else root
        )
        archive_root = Path(project_root) / str(receipt.get("archive_root") or "")
        destinations = [
            *(receipt.get("archived_runtime") or []),
            *(receipt.get("archived_bot_debris") or []),
        ]
        return (
            _fingerprint_file(archive_root / POLICY_EPOCH_RESET_CLAIM_FILENAME),
            _fingerprint_file(
                archive_root / POLICY_EPOCH_RESET_ARCHIVE_RECEIPT_FILENAME
            ),
            tuple(
                _fingerprint_stat(project_root / str(row.get("to") or ""))
                for row in destinations
                if isinstance(row, dict)
            ),
        )
    except (OSError, ValueError):
        return None


def _monitor_pool_fingerprint():
    """Published active pool exactly as the load chain re-derives it.

    A pool change without a following cycle commit must still drop the
    cached build, because the loader would now fail closed (pool mismatch or
    empty pool) for the new pool.  Discovery is TTL cached, so the per-tick
    cost is one cache lookup plus a shared git fan-out per TTL window.
    """
    try:
        from evolution_infra import get_published_active_bots_read_only

        return tuple(sorted(get_published_active_bots_read_only(ledger_fresh=True)))
    except Exception:
        return None


def _monitor_bundle_fingerprint(root):
    return (
        _fingerprint_file(root / "policy_epoch_reset_receipt.json"),
        _fingerprint_file(root / "evaluation_data_manifest.json"),
        _fingerprint_file(root / "evaluation_cycle_manifest.json"),
        _monitor_epoch_archive_fingerprint(root),
        _monitor_semantic_fingerprint(),
        _monitor_pool_fingerprint(),
        _fingerprint_tree(root / "evaluation_cycles"),
        _fingerprint_tree(root / "match_replay"),
    )


def monitor_strict_evaluation_bundle(results_dir=None):
    """Return the fingerprint-gated strict bundle read projection.

    While every consumed input still carries the fingerprint of the last
    successful build, the previous bundle dict is reused and the expensive
    evidence re-verification is skipped.  Only this read projection is
    cached, and only successful (``available: True``) builds are cached.
    While a bundle is available, the dashboard's stats/ratings are the
    cycle-frozen figures inside it (WebUI.update_daemon_status ignores the
    freshly polled per-tick values), so they refresh when the projection
    rebuilds — a committed evidence change — not on every monitor tick.
    """
    root = Path(results_dir) if results_dir is not None else RESULTS_DIR
    cache_key = str(root)
    fingerprint = _monitor_bundle_fingerprint(root)
    with _DAEMON_MONITOR_BUNDLE_LOCK:
        if _DAEMON_MONITOR_BUNDLE_CACHE["valid"]:
            if (
                _DAEMON_MONITOR_BUNDLE_CACHE["root"] == cache_key
                and _DAEMON_MONITOR_BUNDLE_CACHE["fingerprint"] == fingerprint
            ):
                return _DAEMON_MONITOR_BUNDLE_CACHE["bundle"]
        # Stale entry: never serve it, and never serve a half-updated cache.
        _DAEMON_MONITOR_BUNDLE_CACHE["valid"] = False
    try:
        from evaluation_bundle import load_current_strict_evaluation_bundle

        bundle = load_current_strict_evaluation_bundle(root)
    except Exception:
        # Fail closed without caching the failure: the next tick retries.
        return {"available": False}
    if not isinstance(bundle, dict) or bundle.get("available") is not True:
        # The loader reports most failures as reason dicts rather than
        # raising; those stay uncached too, so a transient failure (pool
        # discovery, concurrent publish) self-heals on the next tick.
        return bundle if isinstance(bundle, dict) else {"available": False}
    with _DAEMON_MONITOR_BUNDLE_LOCK:
        _DAEMON_MONITOR_BUNDLE_CACHE["root"] = cache_key
        _DAEMON_MONITOR_BUNDLE_CACHE["fingerprint"] = fingerprint
        _DAEMON_MONITOR_BUNDLE_CACHE["bundle"] = bundle
        _DAEMON_MONITOR_BUNDLE_CACHE["valid"] = True
    return bundle


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
        daemon_workers = default_daemon_workers()
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
                if not shutting_down:
                    try:
                        from stability_observation import (
                            reset_stability_observation,
                        )

                        reset_stability_observation(
                            "rating_daemon_exited",
                            details={
                                "pid": proc.pid,
                                "returncode": rc,
                                "replacement_pid": (
                                    current_proc.pid
                                    if current_proc is not None
                                    and current_proc is not proc
                                    and current_proc.poll() is None
                                    else None
                                ),
                            },
                        )
                    except Exception as exc:
                        try:
                            log_system_event(
                                "daemon.stability_observation_failed",
                                "error",
                                "Rating daemon exited but stability reset failed",
                                {
                                    "pid": proc.pid,
                                    "returncode": rc,
                                    "error_type": type(exc).__name__,
                                    "error": str(exc)[:300],
                                },
                            )
                        except Exception:
                            pass
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
            bundle = monitor_strict_evaluation_bundle()
            ui.update_daemon_status(stats, ratings, strict_bundle=bundle)
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
        stop_event.wait(DAEMON_MONITOR_INTERVAL_SEC)
