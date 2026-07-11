"""Durable state machine for long-running official EXE certification."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import secrets
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

from bot_artifact import canonical_digest


JOB_SCHEMA_VERSION = 3
JOB_MANAGER_VERSION = "official-job-v3"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_WORKER_RESTARTS = 3
HEARTBEAT_INTERVAL_SEC = 5.0
HEARTBEAT_LEASE_SEC = 30.0
MIN_PROGRESS_LEASE_SEC = 180.0
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
PENDING_STATES = frozenset({"queued", "starting", "running", "finalizing", "cancel_requested"})

ROOT = Path(__file__).resolve().parents[2]
SERVICE_PATH = Path(__file__).resolve()
_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


class OfficialJobCancelled(BaseException):
    """Signal-safe cancellation that still executes harness cleanup blocks."""


def job_root() -> Path:
    from official_certification import certification_root

    return Path(os.environ.get("POK_OFFICIAL_JOB_DIR", str(certification_root() / "jobs")))


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock(path):
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _index_lock():
    with _file_lock(job_root() / "index.lock"):
        yield


@contextmanager
def _job_lock(directory: Path):
    with _file_lock(directory / "job.lock"):
        yield


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"official job payload is not an object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _stable_opponent_selection(selection: dict[str, Any] | None) -> dict[str, Any] | None:
    from official_certification import stable_official_opponent_selection

    return stable_official_opponent_selection(selection)


def _request_payload(spec, *, opponent_selection, source_v) -> dict[str, Any]:
    from dataclasses import asdict
    from official_certification import certification_identity

    identity = certification_identity(spec)
    payload = {
        "schema_version": JOB_SCHEMA_VERSION,
        "manager_version": JOB_MANAGER_VERSION,
        "spec": asdict(spec),
        "identity": identity,
        # Volatile diagnostics such as `considered` and readiness counts must
        # not create a new request on every poll. The selected content-bound
        # opponent receipt is the only selection input that affects the run.
        "opponent_selection": _stable_opponent_selection(opponent_selection),
        "source_v": int(source_v) if source_v is not None else None,
        "manager_sha256": hashlib.sha256(SERVICE_PATH.read_bytes()).hexdigest(),
    }
    payload["request_digest"] = canonical_digest(payload)
    payload["job_id"] = canonical_digest({"request_digest": payload["request_digest"]})
    return payload


def _validate_request(payload: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if payload.get("schema_version") != JOB_SCHEMA_VERSION:
        issues.append("official_job_request_schema_mismatch")
    if payload.get("manager_version") != JOB_MANAGER_VERSION:
        issues.append("official_job_manager_version_mismatch")
    expected = canonical_digest({
        key: value
        for key, value in payload.items()
        if key not in {"request_digest", "job_id"}
    })
    if payload.get("request_digest") != expected:
        issues.append("official_job_request_digest_mismatch")
    if payload.get("job_id") != canonical_digest({"request_digest": payload.get("request_digest")}):
        issues.append("official_job_identity_mismatch")
    return issues


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except Exception:
        return "unknown"


def _proc_stat(pid: int) -> tuple[str, int] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = raw[raw.rfind(")") + 2 :].split()
        return fields[19], int(fields[2])  # start ticks, process group id
    except Exception:
        return None


def _proc_start_ticks(pid: int) -> str:
    stat = _proc_stat(pid)
    return stat[0] if stat else ""


def _process_alive(state: dict[str, Any], directory: Path) -> bool:
    try:
        pid = int(state.get("pid") or 0)
        pgid = int(state.get("pgid") or 0)
    except (TypeError, ValueError):
        return False
    stat = _proc_stat(pid)
    if (
        pid <= 1
        or stat is None
        or stat[0] != str(state.get("pid_start_ticks") or "")
        or stat[1] != pgid
        or pgid != pid
        or state.get("boot_id") != _boot_id()
    ):
        return False
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", errors="replace"
        )
    except Exception:
        return False
    return (
        str(SERVICE_PATH) in command
        and str(directory) in command
        and str(state.get("claim_token") or "") in command
    )


def _safe_group_members(pgid: int) -> list[int]:
    members: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return members
    for entry in entries:
        if not entry.name.isdigit():
            continue
        stat = _proc_stat(int(entry.name))
        if stat is not None and stat[1] == pgid:
            members.append(int(entry.name))
    return members


def _member_owned_by_job(pid: int, state: dict[str, Any], directory: Path) -> bool:
    try:
        worker_pid = int(state.get("pid") or 0)
        expected_pgid = int(state.get("pgid") or 0)
    except (TypeError, ValueError):
        worker_pid = 0
        expected_pgid = 0
    if pid == worker_pid:
        stat = _proc_stat(pid)
        return bool(
            stat is not None
            and stat[0] == str(state.get("pid_start_ticks") or "")
            and stat[1] == expected_pgid
            and state.get("boot_id") == _boot_id()
        )
    try:
        values = set(Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"))
    except OSError:
        return False
    expected = {
        b"POK_OFFICIAL_JOB_CLAIM_TOKEN=" + str(state.get("claim_token") or "").encode(),
        b"POK_OFFICIAL_JOB_DIRECTORY=" + str(directory).encode(),
    }
    return expected.issubset(values)


def _terminate_worker_group(state: dict[str, Any], directory: Path, *, grace_sec: float = 12.0) -> None:
    try:
        pgid = int(state.get("pgid") or state.get("pid") or 0)
    except (TypeError, ValueError):
        return
    if pgid <= 1:
        return
    alive = _process_alive(state, directory)
    members = _safe_group_members(pgid)
    if not alive and not members:
        return
    if not alive:
        if not all(_member_owned_by_job(pid, state, directory) for pid in members):
            return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + max(0.0, grace_sec)
    while time.time() < deadline and _safe_group_members(pgid):
        time.sleep(0.1)
    if _safe_group_members(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _suite_dir(directory: Path, attempt: int) -> Path:
    return directory / f"suite_attempt_{max(1, int(attempt)):02d}"


def _live_log_progress(round_dir: Path) -> tuple[int, int, int]:
    values: list[tuple[int, int, int]] = []
    for name in ("botA.log", "botB.log"):
        path = round_dir / name
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        values.append((
            text.count("DISPATCH line='preflop|"),
            text.count("DISPATCH line='earnChips"),
            len(text.encode("utf-8", errors="replace")),
        ))
    if not values:
        return 0, 0, 0
    return min(item[0] for item in values), min(item[1] for item in values), sum(item[2] for item in values)


def _scan_progress(directory: Path, request: dict[str, Any], attempt: int = 1) -> dict[str, Any]:
    spec = request.get("spec") or {}
    total = int(spec.get("self_play_rounds", 0) or 0) + int(spec.get("opponent_rounds", 0) or 0)
    suite = _suite_dir(directory, attempt)
    completed = passed = 0
    active = None
    rounds: list[dict[str, Any]] = []
    for kind, count in (
        ("self_play", int(spec.get("self_play_rounds", 0) or 0)),
        ("opponent", int(spec.get("opponent_rounds", 0) or 0)),
    ):
        for index in range(1, count + 1):
            round_dir = suite / f"{kind}_{index:02d}"
            try:
                receipt = _read_json(round_dir / "receipt.json")
            except Exception:
                receipt = None
            if not receipt:
                if round_dir.is_dir():
                    hands, settlements, observed_bytes = _live_log_progress(round_dir)
                    if hands or settlements or observed_bytes:
                        row = {
                            "kind": kind,
                            "index": index,
                            "passed": False,
                            "hands_started": hands,
                            "settlements": settlements,
                            "observed_bytes": observed_bytes,
                            "duration_sec": None,
                            "issue_count": 0,
                        }
                        rounds.append(row)
                        active = row
                continue
            summary = receipt.get("log_summary") or {}
            hands = int(summary.get("hands_started_min", 0) or 0)
            settlements = int(summary.get("settlements_min", 0) or 0)
            wire_bytes = 0
            if receipt.get("duration_sec") is None:
                hands, settlements, wire_bytes = _live_log_progress(round_dir)
            row = {
                "kind": kind,
                "index": index,
                "passed": bool(receipt.get("passed")),
                "hands_started": hands,
                "settlements": settlements,
                "observed_bytes": wire_bytes,
                "duration_sec": receipt.get("duration_sec"),
                "issue_count": len(receipt.get("issues") or []),
            }
            rounds.append(row)
            if receipt.get("duration_sec") is not None:
                completed += 1
                passed += int(bool(receipt.get("passed")))
            else:
                active = row
    return {
        "suite_attempt": attempt,
        "rounds_requested": total,
        "rounds_completed": completed,
        "rounds_passed": passed,
        "active_round": active,
        "rounds": rounds,
    }


def _result_path(directory: Path, attempt: int) -> Path:
    return directory / f"result_attempt_{max(1, int(attempt)):02d}.json"


def _result_payload(directory: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    path_value = state.get("result_path")
    path = Path(str(path_value)) if path_value else _result_path(directory, int(state.get("attempt", 1) or 1))
    payload = _read_json(path)
    if not payload:
        return None
    digest = canonical_digest({key: value for key, value in payload.items() if key != "result_digest"})
    if payload.get("result_digest") != digest:
        raise ValueError("official_job_result_digest_mismatch")
    if state.get("result_digest") and state.get("result_digest") != digest:
        raise ValueError("official_job_state_result_digest_mismatch")
    request = _read_json(directory / "request.json") or {}
    if payload.get("schema_version") != JOB_SCHEMA_VERSION:
        raise ValueError("official_job_result_schema_mismatch")
    if payload.get("job_id") != state.get("job_id") or payload.get("job_id") != request.get("job_id"):
        raise ValueError("official_job_result_identity_mismatch")
    if (
        payload.get("request_digest") != state.get("request_digest")
        or payload.get("request_digest") != request.get("request_digest")
    ):
        raise ValueError("official_job_result_request_mismatch")
    if int(payload.get("attempt", 0) or 0) != int(state.get("attempt", 0) or 0):
        raise ValueError("official_job_result_attempt_mismatch")
    if not isinstance(payload.get("status"), dict):
        raise ValueError("official_job_result_status_invalid")
    return payload


def _bump_state(current: dict[str, Any], **updates: Any) -> dict[str, Any]:
    now = time.time()
    result = {**current, **updates}
    result["revision"] = int(current.get("revision", 0) or 0) + 1
    result["updated_at_epoch"] = now
    return result


def _with_progress(state: dict[str, Any], progress: dict[str, Any]) -> dict[str, Any]:
    digest = canonical_digest(progress)
    updates: dict[str, Any] = {"progress": progress, "progress_digest": digest}
    if digest != state.get("progress_digest"):
        updates["last_progress_at_epoch"] = time.time()
    return _bump_state(state, **updates)


def _spawn_worker(directory: Path, state: dict[str, Any], *, max_attempts: int, new_suite: bool) -> dict[str, Any]:
    attempt = int(state.get("attempt", 0) or 0) + (1 if new_suite else 0)
    attempt = max(1, attempt)
    attempt_nonce = (
        secrets.token_hex(32)
        if new_suite or not str(state.get("attempt_nonce") or "")
        else str(state.get("attempt_nonce"))
    )
    claim_token = uuid.uuid4().hex
    env = os.environ.copy()
    core_path = str(SERVICE_PATH.parent)
    env["PYTHONPATH"] = core_path + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["POK_OFFICIAL_JOB_CLAIM_TOKEN"] = claim_token
    env["POK_OFFICIAL_JOB_DIRECTORY"] = str(directory)
    with (directory / "worker.stdout.log").open("ab") as stdout, (directory / "worker.stderr.log").open("ab") as stderr:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(SERVICE_PATH),
                "--worker",
                str(directory),
                "--claim-token",
                claim_token,
            ],
            cwd=str(ROOT),
            env=env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
    start_ticks = ""
    for _ in range(30):
        start_ticks = _proc_start_ticks(proc.pid)
        if start_ticks:
            break
        time.sleep(0.01)
    now = time.time()
    progress = _scan_progress(directory, _read_json(directory / "request.json") or {}, attempt)
    return _bump_state(
        state,
        schema_version=JOB_SCHEMA_VERSION,
        manager_version=JOB_MANAGER_VERSION,
        state="starting",
        phase="worker_handshake",
        attempt=attempt,
        attempt_nonce=attempt_nonce,
        max_attempts=int(max_attempts),
        worker_restart_count=(0 if new_suite else int(state.get("worker_restart_count", 0) or 0) + 1),
        claim_token=claim_token,
        pid=proc.pid,
        pgid=proc.pid,
        pid_start_ticks=start_ticks,
        boot_id=_boot_id(),
        worker_started_at_epoch=now,
        phase_started_at_epoch=now,
        heartbeat_at_epoch=now,
        last_progress_at_epoch=now,
        progress=progress,
        progress_digest=canonical_digest(progress),
        result_digest=None,
        result_path=None,
        failure=None,
        waiting_for_resource=None,
    )


def _public_state(directory: Path, state: dict[str, Any]) -> dict[str, Any]:
    payload = dict(state)
    payload["job_dir"] = str(directory)
    payload["pending"] = state.get("state") in PENDING_STATES
    if state.get("state") == "completed":
        result = _result_payload(directory, state)
        payload["status"] = (result or {}).get("status")
    return payload


def _another_live_job(current_job_id: str) -> bool:
    root = job_root()
    if not root.is_dir():
        return False
    for directory in root.iterdir():
        if not directory.is_dir() or directory.name == current_job_id:
            continue
        try:
            state = _read_json(directory / "state.json") or {}
        except Exception:
            continue
        if state.get("state") in {"starting", "running", "finalizing", "cancel_requested"} and _process_alive(state, directory):
            return True
    return False


def _healthy_worker(state: dict[str, Any], directory: Path, request: dict[str, Any]) -> tuple[bool, str]:
    if not _process_alive(state, directory):
        return False, "worker_process_not_alive"
    now = time.time()
    if now - float(state.get("heartbeat_at_epoch", 0.0) or 0.0) > HEARTBEAT_LEASE_SEC:
        return False, "worker_heartbeat_lease_expired"
    no_progress = float((request.get("spec") or {}).get("no_progress_timeout_sec", 75.0) or 75.0)
    progress_lease = max(MIN_PROGRESS_LEASE_SEC, no_progress * 2.0 + 30.0)
    progress = state.get("progress") or {}
    if (
        int(progress.get("rounds_completed", 0) or 0) < int(progress.get("rounds_requested", 0) or 0)
        and now - float(state.get("last_progress_at_epoch", 0.0) or 0.0) > progress_lease
    ):
        return False, "worker_progress_lease_expired"
    return True, "healthy"


def _adopt_result_locked(directory: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    path = _result_path(directory, int(state.get("attempt", 1) or 1))
    try:
        payload = _result_payload(directory, {**state, "result_path": str(path)})
    except Exception:
        return None
    if not payload:
        return None
    digest = str(payload["result_digest"])
    return _bump_state(
        state,
        state="completed",
        phase="completed",
        result_path=str(path),
        result_digest=digest,
        heartbeat_at_epoch=time.time(),
        failure=None,
    )


def start_or_poll_job(
    spec,
    *,
    opponent_selection: dict[str, Any] | None = None,
    source_v: int | None = None,
    retry_terminal: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Ensure one identity-bound job and reconcile it without blocking on EXE work."""
    request = _request_payload(spec, opponent_selection=opponent_selection, source_v=source_v)
    directory = job_root() / request["job_id"]
    stale_state: dict[str, Any] | None = None
    with _index_lock():
        with _job_lock(directory):
            request_path = directory / "request.json"
            existing_request = _read_json(request_path)
            if existing_request is None:
                _write_json(request_path, request)
            elif existing_request != request:
                return {
                    "state": "failed",
                    "failure_class": "infrastructure",
                    "issues": ["official_job_request_identity_collision"],
                    "job_id": request["job_id"],
                    "job_dir": str(directory),
                }
            request_issues = _validate_request(request)
            if request_issues:
                return {
                    "state": "failed",
                    "failure_class": "infrastructure",
                    "issues": request_issues,
                    "job_id": request["job_id"],
                    "job_dir": str(directory),
                }
            state = _read_json(directory / "state.json") or {
                "schema_version": JOB_SCHEMA_VERSION,
                "manager_version": JOB_MANAGER_VERSION,
                "job_id": request["job_id"],
                "candidate": str((request.get("spec") or {}).get("candidate") or ""),
                "request_digest": request["request_digest"],
                "state": "created",
                "phase": "created",
                "attempt": 0,
                "worker_restart_count": 0,
                "max_attempts": int(max_attempts),
                "revision": 0,
                "created_at_epoch": time.time(),
            }
            if state.get("job_id") != request["job_id"] or state.get("request_digest") != request["request_digest"]:
                return {
                    "state": "failed",
                    "failure_class": "infrastructure",
                    "issues": ["official_job_state_identity_mismatch"],
                    "job_id": request["job_id"],
                    "job_dir": str(directory),
                }
            if state.get("state") == "completed" and not retry_terminal:
                try:
                    return _public_state(directory, state)
                except Exception as exc:
                    state = _bump_state(
                        state,
                        state="failed",
                        phase="result_validation",
                        failure=f"{type(exc).__name__}: {str(exc)[:300]}",
                    )
                    _write_json(directory / "state.json", state)
                    return {**_public_state(directory, state), "failure_class": "infrastructure"}
            if state.get("state") == "cancelled":
                return _public_state(directory, state)
            if state.get("state") == "cancel_requested":
                # A process may have died after persisting cancel_requested but
                # before the lock-free process-group cleanup/finalization step.
                # Re-enter that step instead of leaving a permanent pending job.
                stale_state = dict(state)
            elif state.get("state") in {"starting", "running", "finalizing"}:
                progress = _scan_progress(directory, request, int(state.get("attempt", 1) or 1))
                state = _with_progress(state, progress)
                healthy, reason = _healthy_worker(state, directory, request)
                if healthy:
                    _write_json(directory / "state.json", state)
                    return _public_state(directory, state)
                adopted = _adopt_result_locked(directory, state)
                if adopted is not None:
                    _write_json(directory / "state.json", adopted)
                    return _public_state(directory, adopted)
                stale_state = dict(state)
                state = _bump_state(
                    state,
                    state="cancel_requested",
                    phase="reconcile_worker",
                    failure=reason,
                    cancel_requested_at_epoch=time.time(),
                )
                _write_json(directory / "state.json", state)
            else:
                if state.get("state") in TERMINAL_STATES and retry_terminal:
                    if int(state.get("attempt", 0) or 0) >= int(max_attempts):
                        return {
                            **_public_state(directory, state),
                            "failure_class": "infrastructure",
                            "issues": [str(state.get("failure") or "official_job_attempts_exhausted")],
                            "exhausted": True,
                        }
                    state = _bump_state(
                        state,
                        state="queued",
                        phase="retry_queued",
                        worker_restart_count=0,
                    )
                elif state.get("state") == "failed" and not retry_terminal:
                    return {**_public_state(directory, state), "failure_class": "infrastructure"}
                elif state.get("state") == "created":
                    state = _bump_state(state, state="queued", phase="queued")
                if _another_live_job(request["job_id"]):
                    _write_json(directory / "state.json", state)
                    return _public_state(directory, state)
                from official_certification import official_lock_busy

                if official_lock_busy():
                    waiting_phase = (
                        "retry_queued"
                        if state.get("phase") == "retry_queued"
                        else "waiting_for_official_platform"
                    )
                    state = _bump_state(
                        state,
                        state="queued",
                        phase=waiting_phase,
                        waiting_for_resource="official_platform",
                    )
                    _write_json(directory / "state.json", state)
                    return _public_state(directory, state)
                new_suite = int(state.get("attempt", 0) or 0) == 0 or state.get("phase") == "retry_queued"
                state = _spawn_worker(
                    directory,
                    state,
                    max_attempts=max_attempts,
                    new_suite=new_suite,
                )
                _write_json(directory / "state.json", state)
                return _public_state(directory, state)

    if stale_state is not None:
        _terminate_worker_group(stale_state, directory)
        with _index_lock():
            with _job_lock(directory):
                state = _read_json(directory / "state.json") or stale_state
                if state.get("claim_token") != stale_state.get("claim_token"):
                    return _public_state(directory, state)
                if state.get("state") == "cancelled":
                    return _public_state(directory, state)
                if state.get("state") != "cancel_requested":
                    return _public_state(directory, state)
                if state.get("phase") == "operator_cancel" or state.get("cancel_reason"):
                    state = _bump_state(
                        state,
                        state="cancelled",
                        phase="cancelled",
                        failure=str(state.get("cancel_reason") or "operator_cancelled"),
                        heartbeat_at_epoch=time.time(),
                    )
                    _write_json(directory / "state.json", state)
                    return _public_state(directory, state)
                adopted = _adopt_result_locked(directory, state)
                if adopted is not None:
                    _write_json(directory / "state.json", adopted)
                    return _public_state(directory, adopted)
                restarts = int(state.get("worker_restart_count", 0) or 0)
                if restarts >= DEFAULT_MAX_WORKER_RESTARTS:
                    state = _bump_state(
                        state,
                        state="failed",
                        phase="worker_restart_exhausted",
                        failure=str(state.get("failure") or "worker_restart_exhausted"),
                    )
                    _write_json(directory / "state.json", state)
                    return {**_public_state(directory, state), "failure_class": "infrastructure"}
                state = _spawn_worker(
                    directory,
                    state,
                    max_attempts=max_attempts,
                    new_suite=False,
                )
                _write_json(directory / "state.json", state)
                return _public_state(directory, state)
    raise RuntimeError("official job reconciliation reached no terminal action")


def cancel_job(job_id: str, *, reason: str = "operator_cancelled", grace_sec: float = 12.0) -> dict[str, Any]:
    directory = job_root() / str(job_id)
    with _job_lock(directory):
        state = _read_json(directory / "state.json") or {}
        if not state:
            return {"state": "missing", "job_id": job_id}
        if state.get("state") in TERMINAL_STATES:
            return _public_state(directory, state)
        target = dict(state)
        state = _bump_state(
            state,
            state="cancel_requested",
            phase="operator_cancel",
            cancel_reason=reason,
            cancel_requested_at_epoch=time.time(),
        )
        _write_json(directory / "state.json", state)
    _terminate_worker_group(target, directory, grace_sec=grace_sec)
    with _job_lock(directory):
        state = _read_json(directory / "state.json") or state
        if state.get("claim_token") == target.get("claim_token"):
            state = _bump_state(
                state,
                state="cancelled",
                phase="cancelled",
                failure=reason,
                heartbeat_at_epoch=time.time(),
            )
            _write_json(directory / "state.json", state)
        return _public_state(directory, state)


def job_snapshot() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    root = job_root()
    if root.is_dir():
        for directory in sorted(root.iterdir()):
            if not directory.is_dir() or directory.is_symlink():
                continue
            try:
                with _job_lock(directory):
                    state = _read_json(directory / "state.json")
                    if state:
                        rows.append(_public_state(directory, state))
            except Exception as exc:
                rows.append({
                    "job_id": directory.name,
                    "state": "failed",
                    "failure_class": "infrastructure",
                    "issues": [f"{type(exc).__name__}: {str(exc)[:200]}"],
                })
    rows.sort(key=lambda item: float(item.get("updated_at_epoch", 0.0) or 0.0), reverse=True)
    return {
        "schema_version": JOB_SCHEMA_VERSION,
        "pending": sum(1 for item in rows if item.get("pending")),
        "running": sum(1 for item in rows if item.get("state") in {"starting", "running", "finalizing"}),
        "jobs": rows,
    }


def get_job(job_id: str) -> dict[str, Any] | None:
    directory = job_root() / str(job_id)
    if not directory.is_dir() or directory.is_symlink():
        return None
    with _job_lock(directory):
        state = _read_json(directory / "state.json")
        return _public_state(directory, state) if state else None


def reconcile_jobs(*, limit: int = 4) -> dict[str, Any]:
    """Poll queued/running jobs through the same manager used by commit_bot."""
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    root = job_root()
    candidates: list[tuple[int, float, str, Path]] = []
    if root.is_dir():
        for directory in root.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            try:
                state = _read_json(directory / "state.json") or {}
            except Exception:
                state = {}
            current = str(state.get("state") or "")
            if current not in PENDING_STATES:
                continue
            active_rank = 0 if current in {"starting", "running", "finalizing", "cancel_requested"} else 1
            lease_epoch = float(
                state.get("heartbeat_at_epoch")
                or state.get("updated_at_epoch")
                or state.get("created_at_epoch")
                or 0.0
            )
            candidates.append((active_rank, lease_epoch, directory.name, directory))
    # Reconcile the active owner before queued followers. Otherwise a fixed
    # lexical prefix of queued jobs can starve a stale owner forever and keep the
    # global EXE slot blocked.
    directories = [item[3] for item in sorted(candidates)]
    for directory in directories:
        if len(results) >= max(1, int(limit)):
            break
        try:
            state = _read_json(directory / "state.json") or {}
            if state.get("state") not in PENDING_STATES:
                continue
            request = _read_json(directory / "request.json") or {}
            from official_certification import _spec_from_mapping

            result = start_or_poll_job(
                _spec_from_mapping(request.get("spec") or {}),
                opponent_selection=request.get("opponent_selection"),
                source_v=request.get("source_v"),
            )
            results.append(result)
        except Exception as exc:
            errors.append(f"{directory.name}:{type(exc).__name__}:{str(exc)[:240]}")
    pending = sum(1 for item in (job_snapshot().get("jobs") or []) if item.get("pending"))
    return {
        "processed": len(results),
        "remaining": pending,
        "results": results,
        "errors": errors,
    }


def _claim_worker(directory: Path, claim_token: str, *, timeout_sec: float = 15.0) -> tuple[dict[str, Any], int]:
    pid = os.getpid()
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        with _job_lock(directory):
            state = _read_json(directory / "state.json") or {}
            if (
                state.get("state") == "starting"
                and state.get("claim_token") == claim_token
                and int(state.get("pid", 0) or 0) == pid
                and state.get("pid_start_ticks") == _proc_start_ticks(pid)
            ):
                state = _bump_state(
                    state,
                    state="running",
                    phase="certification",
                    phase_started_at_epoch=time.time(),
                    heartbeat_at_epoch=time.time(),
                )
                _write_json(directory / "state.json", state)
                return state, int(state.get("attempt", 1) or 1)
        time.sleep(0.05)
    raise RuntimeError("official_job_spawn_handshake_timeout")


def _update_worker_state(
    directory: Path,
    pid: int,
    attempt: int,
    claim_token: str,
    **updates: Any,
) -> bool:
    with _job_lock(directory):
        state = _read_json(directory / "state.json") or {}
        if (
            int(state.get("pid", 0) or 0) != pid
            or int(state.get("attempt", 0) or 0) != attempt
            or state.get("claim_token") != claim_token
            or state.get("state") in TERMINAL_STATES | {"cancel_requested"}
        ):
            return False
        progress = updates.pop("progress", None)
        state = _bump_state(state, **updates)
        if progress is not None:
            state = _with_progress(state, progress)
        _write_json(directory / "state.json", state)
        return True


def _worker_main(directory: Path, claim_token: str) -> int:
    request = _read_json(directory / "request.json") or {}
    issues = _validate_request(request)
    if issues:
        raise RuntimeError("; ".join(issues))
    from official_certification import (
        _spec_from_mapping,
        certification_identity,
        run_identity_bound_certification_job,
    )

    spec = _spec_from_mapping(request.get("spec") or {})
    if certification_identity(spec) != request.get("identity"):
        raise RuntimeError("official_job_runtime_identity_changed")
    state, attempt = _claim_worker(directory, claim_token)
    pid = os.getpid()
    os.environ["POK_OFFICIAL_JOB_PROCESS_GROUP"] = "1"
    stopped = threading.Event()

    def _cancel(_signum, _frame):
        raise OfficialJobCancelled("official certification job cancelled")

    previous_sigterm = signal.signal(signal.SIGTERM, _cancel)
    previous_sigint = signal.signal(signal.SIGINT, _cancel)

    def _heartbeat() -> None:
        while not stopped.wait(HEARTBEAT_INTERVAL_SEC):
            try:
                if not _update_worker_state(
                    directory,
                    pid,
                    attempt,
                    claim_token,
                    heartbeat_at_epoch=time.time(),
                    progress=_scan_progress(directory, request, attempt),
                ):
                    return
            except Exception:
                return

    heartbeat = threading.Thread(target=_heartbeat, name="official-job-heartbeat", daemon=True)
    heartbeat.start()
    try:
        from official_job_envelope import build_job_envelope

        suite_dir = _suite_dir(directory, attempt)
        job_envelope = build_job_envelope(
            request,
            attempt=attempt,
            attempt_nonce=str(state.get("attempt_nonce") or ""),
            suite_dir=suite_dir,
        )
        status = run_identity_bound_certification_job(
            spec,
            expected_identity=request["identity"],
            expected_opponent_selection=request.get("opponent_selection"),
            suite_dir=suite_dir,
            job_envelope=job_envelope,
            force=attempt > 1,
        )
        result = {
            "schema_version": JOB_SCHEMA_VERSION,
            "job_id": request["job_id"],
            "request_digest": request["request_digest"],
            "attempt": attempt,
            "job_envelope_digest": job_envelope["envelope_digest"],
            "status": status,
        }
        result["result_digest"] = canonical_digest(result)
        result_path = _result_path(directory, attempt)
        _write_json(result_path, result)
        _update_worker_state(
            directory,
            pid,
            attempt,
            claim_token,
            state="completed",
            phase="completed",
            result_path=str(result_path),
            result_digest=result["result_digest"],
            heartbeat_at_epoch=time.time(),
            progress=_scan_progress(directory, request, attempt),
            failure=None,
        )
        return 0
    except OfficialJobCancelled:
        return 130
    except BaseException as exc:
        _update_worker_state(
            directory,
            pid,
            attempt,
            claim_token,
            state="failed",
            phase="worker_exception",
            heartbeat_at_epoch=time.time(),
            failure=f"{type(exc).__name__}: {str(exc)[:500]}",
            progress=_scan_progress(directory, request, attempt),
        )
        raise
    finally:
        stopped.set()
        heartbeat.join(timeout=1.0)
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--claim-token", default="")
    args = parser.parse_args(argv)
    if args.worker is None or not args.claim_token:
        parser.error("--worker JOB_DIR and --claim-token TOKEN are required")
    return _worker_main(args.worker.resolve(), args.claim_token)


if __name__ == "__main__":
    raise SystemExit(main())
