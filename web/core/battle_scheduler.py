"""File-based battle job queue for out-of-process daemon execution.

Uses fcntl-locked JSONL files for communication between orchestrator
and the battle execution daemon.
"""

import fcntl
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from evolution_infra import RESULTS_DIR, locked_file

log = logging.getLogger("pok.scheduler")

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

BATTLE_JOBS_FILE = RESULTS_DIR / "battle_jobs.jsonl"
BATTLE_CLAIMED_FILE = RESULTS_DIR / "battle_jobs.claimed"
BATTLE_RESULTS_FILE = RESULTS_DIR / "battle_results.jsonl"
MAX_PENDING_JOBS = 50
BATTLE_JOB_MAX_AGE = 1800  # 30 minutes


# ──────────────────────────────────────────────
# Dataclasses
# ──────────────────────────────────────────────

@dataclass
class BattleJob:
    job_id: str
    bot_a_name: str
    bot_b_name: str
    bot_a_path: str
    bot_b_path: str
    n_pairs: int
    submitted_at: float
    submitted_by: str = "precommit_eval"
    priority: int = 0
    timeout_sec: float = 600.0
    update_ratings: bool = False


@dataclass
class BattleResult:
    job_id: str
    wins_a: int
    wins_b: int
    draws: int
    total: int
    error: str | None = None
    completed_at: float = field(default_factory=time.time)
    source: str = "scheduler"


# ──────────────────────────────────────────────
# Low-level JSONL helpers
# ──────────────────────────────────────────────

def _append_jsonl(path: Path, records: list[dict]) -> None:
    """Atomically append records to a JSONL file with fsync.

    Uses an exclusive lock to prevent interleaved writes from concurrent
    processes.
    """
    with locked_file(path, "a") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _read_jsonl(path: Path) -> list[dict]:
    """Read all valid JSON lines from *path*.

    Skips malformed lines and logs a warning for each one.  Uses a shared
    lock so readers do not block each other.
    """
    results: list[dict] = []
    if not path.exists():
        return results
    with locked_file(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("Malformed JSON line in %s: %s", path, line[:200])
    return results


def _write_jsonl_atomic(path: Path, records: list[dict]) -> None:
    """Replace the contents of *path* with *records* atomically under LOCK_EX.

    Writes to a temporary file in the same directory, fsyncs, then atomically
    renames into place.  This avoids data loss if the process crashes mid-write.
    """
    dir_path = path.parent
    dir_path.mkdir(parents=True, exist_ok=True)

    # Write to a temp file in the same directory so os.replace is atomic.
    fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as tmp_f:
            for rec in records:
                tmp_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            tmp_f.flush()
            os.fsync(tmp_f.fileno())
        # Atomically replace the target under an exclusive lock.
        with locked_file(path, "a+", lock_type=fcntl.LOCK_EX) as f:
            os.replace(tmp_path, str(path))
            tmp_path = None  # prevent cleanup in finally
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def submit_jobs(jobs: list[BattleJob]) -> list[str]:
    """Submit one or more battle jobs to the pending queue.

    Raises RuntimeError if the total number of pending jobs would exceed
    MAX_PENDING_JOBS.
    """
    records = []
    job_ids = []
    for job in jobs:
        if not job.job_id:
            job.job_id = str(uuid.uuid4())
        rec = asdict(job)
        records.append(rec)
        job_ids.append(job.job_id)

    # Count and append under a single exclusive lock to prevent TOCTOU.
    with locked_file(BATTLE_JOBS_FILE, "a+", lock_type=fcntl.LOCK_EX) as f:
        f.seek(0)
        existing_count = sum(1 for line in f if line.strip())
        if existing_count + len(jobs) > MAX_PENDING_JOBS:
            raise RuntimeError(
                f"Pending job limit exceeded: {existing_count} + {len(jobs)} > {MAX_PENDING_JOBS}"
            )
        f.seek(0, 2)  # seek to end
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())

    log.info("Submitted %d battle job(s): %s", len(jobs), job_ids)
    return job_ids


def drain_pending_jobs() -> list[dict]:
    """Daemon side: move valid pending jobs into the claimed set.

    Reads all pending jobs, filters out jobs whose bot files no longer exist
    or that have expired, writes error results for the invalid ones, moves
    the valid jobs to BATTLE_CLAIMED_FILE, and truncates the pending file.

    Returns the list of valid job dicts that were claimed.
    """
    now = time.time()
    valid: list[dict] = []
    error_results: list[dict] = []
    raw_lines: list[str] = []

    # Read and truncate under a single exclusive lock to prevent TOCTOU.
    with locked_file(BATTLE_JOBS_FILE, "a+", lock_type=fcntl.LOCK_EX) as f:
        f.seek(0)
        for line in f:
            raw_lines.append(line)
        # Always truncate, even if no valid jobs — clears stale/expired entries.
        f.seek(0)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())

    # Parse lines outside the lock (data is ours now).
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            job = json.loads(line)
        except json.JSONDecodeError:
            log.warning("Malformed JSON line in drain: %s", line[:200])
            continue

        job_id = job.get("job_id", "")
        bot_a = job.get("bot_a_path", "")
        bot_b = job.get("bot_b_path", "")

        # Check bot files exist
        if bot_a and not Path(bot_a).exists():
            error_results.append(
                asdict(
                    BattleResult(
                        job_id=job_id,
                        wins_a=0,
                        wins_b=0,
                        draws=0,
                        total=0,
                        error="not_found",
                        completed_at=now,
                    )
                )
            )
            continue
        if bot_b and not Path(bot_b).exists():
            error_results.append(
                asdict(
                    BattleResult(
                        job_id=job_id,
                        wins_a=0,
                        wins_b=0,
                        draws=0,
                        total=0,
                        error="not_found",
                        completed_at=now,
                    )
                )
            )
            continue

        # Check expiry
        submitted_at = job.get("submitted_at", 0)
        if now - submitted_at > BATTLE_JOB_MAX_AGE:
            error_results.append(
                asdict(
                    BattleResult(
                        job_id=job_id,
                        wins_a=0,
                        wins_b=0,
                        draws=0,
                        total=0,
                        error="expired",
                        completed_at=now,
                    )
                )
            )
            continue

        valid.append(job)

    # Write side-effects outside the jobs-file lock.
    if error_results:
        _append_jsonl(BATTLE_RESULTS_FILE, error_results)

    if valid:
        _append_jsonl(BATTLE_CLAIMED_FILE, valid)

    log.info("Drained %d pending jobs, claimed %d valid", len(raw_lines), len(valid))
    return valid


def ack_claimed(job_id: str) -> None:
    """Daemon side: remove a job from the claimed file after it completes.

    Reads BATTLE_CLAIMED_FILE, filters out the given job_id, and writes
    the remainder back atomically.
    """
    claimed = _read_jsonl(BATTLE_CLAIMED_FILE)
    filtered = [c for c in claimed if c.get("job_id") != job_id]
    if len(filtered) != len(claimed):
        _write_jsonl_atomic(BATTLE_CLAIMED_FILE, filtered)
        log.debug("Acknowledged claimed job %s", job_id)


def write_result(result: BattleResult) -> None:
    """Daemon side: append a completed battle result and ack the job.

    The result is written to BATTLE_RESULTS_FILE and the corresponding
    job_id is removed from BATTLE_CLAIMED_FILE.
    """
    _append_jsonl(BATTLE_RESULTS_FILE, [asdict(result)])
    ack_claimed(result.job_id)
    log.info("Wrote result for job %s", result.job_id)


def collect_results(job_ids: list[str]) -> dict[str, dict]:
    """Caller side: collect results for the given job_ids.

    Reads all results, returns a dict mapping job_id to result dict for
    the requested ids, and atomically writes back only the uncollected
    results so that repeated calls are idempotent.
    """
    if not job_ids:
        return {}

    job_id_set = set(job_ids)
    all_results = _read_jsonl(BATTLE_RESULTS_FILE)
    collected: dict[str, dict] = {}
    uncollected: list[dict] = []

    for rec in all_results:
        jid = rec.get("job_id", "")
        if jid in job_id_set:
            collected[jid] = rec
        else:
            uncollected.append(rec)

    _write_jsonl_atomic(BATTLE_RESULTS_FILE, uncollected)
    log.info("Collected %d/%d requested results", len(collected), len(job_ids))
    return collected


def cleanup_stale(max_age_sec: int = 3600) -> int:
    """Remove result records older than *max_age_sec*.

    Returns the number of records removed.
    """
    now = time.time()
    all_results = _read_jsonl(BATTLE_RESULTS_FILE)
    kept: list[dict] = []
    removed = 0

    for rec in all_results:
        completed_at = rec.get("completed_at", 0)
        if not completed_at:
            completed_at = now
        if now - completed_at > max_age_sec:
            removed += 1
        else:
            kept.append(rec)

    if removed:
        _write_jsonl_atomic(BATTLE_RESULTS_FILE, kept)
        log.info("Cleaned up %d stale result records", removed)
    return removed


def requeue_unclaimed_on_startup() -> list[dict]:
    """Daemon startup: return orphaned claimed jobs that have no result.

    Reads the claimed file and the results file, collects all job_ids that
    already have results, and returns claimed jobs that do NOT have results.
    These are jobs that were claimed by a previous daemon process but never
    completed (e.g. after a crash).
    """
    claimed = _read_jsonl(BATTLE_CLAIMED_FILE)
    results = _read_jsonl(BATTLE_RESULTS_FILE)
    result_job_ids = {r.get("job_id", "") for r in results}

    orphaned = [c for c in claimed if c.get("job_id", "") not in result_job_ids]
    log.info("Found %d orphaned claimed job(s)", len(orphaned))
    return orphaned
