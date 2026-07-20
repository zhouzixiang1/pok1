"""Shared infrastructure for the poker bot evolution framework.

Contains constants, file utilities, git operations, ratings helpers.
No LLM agent logic — agent functions live in agent_*.py modules.
Runtime operations (daemon, LLM query, code verification) extracted to
daemon_management.py, llm_query.py, and code_verification.py.
"""

import os
import json
import logging
import shutil
import subprocess
import re
import asyncio
import threading
import uuid
import hashlib
import stat
from copy import deepcopy


import fcntl
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("pok.infra")

# Local module imports (same directory)
from glicko2 import Glicko2Player, update_rating_period
from evaluation_contract import build_evaluation_contract
from evolution_scope import classify_status_entries
from publish_reconcile import reconcile_push_refs
from bot_namespace import (
    ACTIVE_BOT_PREFIX,
    ACTIVE_TAG_PREFIX,
    ARCHIVED_VERSION_HIGH_WATER,
    EVALUATION_EPOCH,
    FIRST_STRICT_POLICY_VERSION,
    NATIONAL_ENTRYPOINT,
    ROLE_CANDIDATE,
    ROLE_PARENT_SOURCE,
    active_bot_glob,
    bot_name,
    bot_relpath,
    bot_tag,
    bot_tag_glob,
    format_version,
    parse_bot_version,
    parse_tag_version,
    resolve_version_namespace_authority,
    resolve_national_bot_spec,
    strip_bot_path_prefix,
    version_sort_key,
)

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

CORE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CORE_DIR.parent.parent
_COPY_IGNORE = shutil.ignore_patterns('__pycache__', '*.pyc')
CANDIDATE_COPY_IGNORE_NAMES = frozenset({"__pycache__", ".completed", ".task_context"})
PROMPTS_DIR = CORE_DIR / "prompts"
RESULTS_DIR = CORE_DIR / "results"
BOTS_DIR = PROJECT_ROOT / "bots"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
STATS_FILE = RESULTS_DIR / "elo_daemon_stats.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"
BOT_STATS_FILE = RESULTS_DIR / "bot_stats.json"
WORKER_FAILURES_FILE = RESULTS_DIR / "worker_failures.jsonl"
PIPELINE_STATE_FILE = RESULTS_DIR / "pipeline_state.json"
REPLAY_DIR = RESULTS_DIR / "match_replay"
MATCH_HISTORY_FILE = RESULTS_DIR / "match_history.jsonl"
ARCHIVE_DIR = RESULTS_DIR / "archive"
POST_PUBLICATION_HANDOFF_DIR = RESULTS_DIR / "post_publication_handoffs"
LLM_COSTS_FILE = RESULTS_DIR / "llm_costs.jsonl"
RATING_HISTORY_FILE = RESULTS_DIR / "rating_history.jsonl"
ABANDONED_VERSIONS_FILE = RESULTS_DIR / "abandoned_versions.jsonl"
REAPED_BOTS_FILE = RESULTS_DIR / "reaped_bots.jsonl"

POST_PUBLICATION_HANDOFF_SCHEMA_VERSION = 2
POST_PUBLICATION_HANDOFF_KIND = "national-policy-post-publication-handoff"
POST_PUBLICATION_HANDOFF_REQUIRED_STEPS = (
    "stability_observation",
    "reap_signal",
    "priority_eval",
    "archive_rotation",
    "log_cleanup",
    "pool_reap",
    "cycle_annotation",
    "housekeeping",
)
POST_PUBLICATION_HANDOFF_STALE_SEC = 15 * 60

ABANDONED_VERSION_RECEIPT_SCHEMA_VERSION = 1
ABANDONED_VERSION_RECEIPT_KIND = "national-policy-abandon-receipt"
_ABANDONED_VERSION_RECEIPT_KEYS = frozenset({
    "schema_version",
    "kind",
    "evaluation_epoch",
    "version",
    "source_v",
    "checkpoint_stage",
    "workflow_run_id",
    "checkpoint_revision",
    "checkpoint_envelope",
    "reason",
    "timestamp",
    "infra_failure",
    "previous_receipt_digest",
    "receipt_digest",
})
_ABANDONED_CHECKPOINT_ENVELOPE_KEYS = frozenset({
    "checkpoint_schema_version",
    "evaluation_epoch",
    "next_v",
    "source_v",
    "parent2_v",
    "generation_mode",
    "epoch_binding",
    "audit_context",
})
_ABANDONED_VERSION_LEDGER_MAX_BYTES = 16 * 1024 * 1024
_ABANDONED_VERSION_REASON_MAX_BYTES = 4 * 1024
_ABANDONED_VERSION_INFRA_FAILURE_MAX_BYTES = 64 * 1024


class AbandonedVersionLedgerError(RuntimeError):
    """The current-epoch allocation receipt ledger is not trustworthy."""

MAX_ACTIVE_BOTS = 30

# Evaluation & quality thresholds
DAEMON_EVAL_TIMEOUT = 600
MIN_GAMES_FOR_EVAL = 100
EVAL_WAIT_PROGRESS_INTERVAL_SEC = int(os.environ.get("POK_EVAL_WAIT_PROGRESS_INTERVAL_SEC", "30"))
MAX_LINES_PER_FILE = 2000       # Candidate-owned policy.py — base limit
MAX_LINES_HELPER = 1500         # System-owned runtime modules — base limit
MAX_LINES_HARD_CAP = 2500       # Hard cap: no .py file may exceed this, even with adaptive budget
LINE_GROWTH_BUDGET = 0.15       # Adaptive limit = max(base, source_lines * (1 + budget))

# Strip a trailing status annotation the LLM may append to target_files entries,
# e.g. "policy.py (MODIFIED)". Requires a bare keyword inside the brackets so
# legitimate names like "report(2).py" survive.
_TARGET_ANNOTATION_RE = re.compile(
    r"\s*[\(\[](?:NEW|CREATE|DELETE|MODIFIED)[\)\]]\s*$", re.IGNORECASE
)
CORE_STRATEGY_FILES = {"policy.py"}
MIN_DECISION_PASS_RATE = 0.7
MIN_CROSSOVER_DECISION_RATE = 0.6
MAX_WORKER_RETRIES = 4
MAX_MASTER_RETRIES = 3
MAX_CROSSOVER_RETRIES = 3
MAX_GENESIS_RETRIES = 3
MAX_PRECOMMIT_RETRIES = 3   # Max run_precommit_eval attempts against the SAME bot code (resets on worker rework)
MAX_PRECOMMIT_REWORK_ROUNDS = int(os.environ.get("POK_MAX_PRECOMMIT_REWORK_ROUNDS", "3"))
MAX_OFFICIAL_REWORK_ROUNDS = int(os.environ.get("POK_MAX_OFFICIAL_REWORK_ROUNDS", "2"))
MAX_MASTER_AUDIT_RETRIES = 1  # Initial Master plan + one corrective re-plan only
WORKER_TIMEOUT = 1000         # Seconds before a hung worker call is aborted + retried
MAX_PARALLEL_WORKERS = 3      # Hard cap on simultaneous LLM worker calls (Semaphore)

# Prompt size limits — Sonnet supports 200K tokens (~800K chars); leave generous headroom
MAX_PROMPT_CHARS = 700_000

# Pipeline stage constants. Re-exported here for compatibility; authoritative
# definitions live in pipeline_state.py.
from pipeline_state import (
    STAGE_ORDER,
    STAGE_GATE_ALLOWLIST,
    validate_stage_transition,
    validate_runtime_contract_ledger_reset,
    is_rework_reset_transition,
    invalidates_official_job_transition,
)

EVOLUTION_BRANCH = "main"


def is_candidate_copy_ignored_name(name: str) -> bool:
    """Return True for parent artifacts that must not enter a new candidate."""

    return name in CANDIDATE_COPY_IGNORE_NAMES or name.endswith(".pyc")


def candidate_copy_ignore(_src: str, names: list[str]) -> set[str]:
    return {name for name in names if is_candidate_copy_ignored_name(name)}


def copy_bot_tree_for_candidate(source_dir: str | Path, target_dir: str | Path) -> None:
    """Copy a completed parent bot into a mutable candidate directory.

    Runtime/task metadata is deliberately excluded. In particular,
    ``.task_context`` files are generated per version by ``plan_compiler`` and
    must never be inherited from an older source bot.
    """

    shutil.copytree(source_dir, target_dir, ignore=candidate_copy_ignore)

# Watchdog: if no pipeline stage change occurs within this many seconds,
# the orchestrator watchdog will clear the session and restart from checkpoint.
# Must exceed typical cycle time (up to 75 min) to avoid false positives.
# Watchdog is secondary safety net; CYCLE_TIMEOUT (60 min) is primary.
WATCHDOG_TIMEOUT = 4500  # 75 minutes

# MCP servers to block for sub-agents (keep zai-mcp-server for vision, block the rest)
_BLOCKED_MCP_TOOLS = [
    "mcp__web-reader__webReader",
    "mcp__web-search-prime__web_search_prime",
    "mcp__zread__get_repo_structure",
    "mcp__zread__read_file",
    "mcp__zread__search_doc",
    # P1 (2026-06-29): disable built-in Task tools for the orchestrator. Task
    # sub-agents do NOT inherit the PreToolUse guard hook, so the LLM could
    # spawn a Task sub-agent whose Bash/Edit writes bot code / pipeline state
    # without any gate check — a full bypass of the pipeline guard. The
    # orchestrator's work is done via MCP tools (run_master/execute_workers/...),
    # so it never legitimately needs Task sub-agents.
    "Task", "TaskCreate", "TaskUpdate", "TaskOutput", "TaskList", "TaskGet",
]

# Adaptive semaphores keyed by api_concurrency level — created on first use
# inside the event loop. level 0 = MAX_PARALLEL_WORKERS(满并发); 503/限速时 level
# 升 → limit = max(1, base >> level) 自动降 worker 并发。不同 level 用不同
# Semaphore 实例(换实例时旧 in-flight worker 自然完成,平滑降级,不丢计数)。
# 这是真正的 LLM 并发源:workers 通过 run_claude_query 打 gateway。
_WORKER_SEMAPHORE: "dict[int, asyncio.Semaphore]" = {}


def _get_worker_semaphore() -> "asyncio.Semaphore":
    """Return (creating if needed) the worker semaphore for the current adaptive
    level. 503/限速时 level 升 → worker 并发自动降(base>>level)。"""
    try:
        from api_concurrency import get_adaptive_limit, get_level
        _lvl = get_level()
        _limit = get_adaptive_limit(MAX_PARALLEL_WORKERS)
    except Exception:
        _lvl, _limit = 0, MAX_PARALLEL_WORKERS
    sem = _WORKER_SEMAPHORE.get(_lvl)
    if sem is None:
        sem = asyncio.Semaphore(_limit)
        _WORKER_SEMAPHORE[_lvl] = sem
    return sem


_FILE_THREAD_LOCKS: dict[str, threading.RLock] = {}
_FILE_THREAD_LOCKS_GUARD = threading.Lock()
_STATE_SIDECAR_LOCAL = threading.local()


def _thread_lock_for(path) -> threading.RLock:
    key = str(Path(path).resolve())
    with _FILE_THREAD_LOCKS_GUARD:
        return _FILE_THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _locked_file_os(path, mode='r', lock_type=None, encoding=None):
    """Context manager for file operations with fcntl locking.

    For mode='w': opens with 'r+' if file exists (to avoid truncating before
    the lock is acquired), then truncates after locking. If file doesn't exist,
    uses 'w' to create it (safe — no data to lose).
    """
    if lock_type is None:
        lock_type = fcntl.LOCK_EX if ('w' in mode or 'a' in mode or '+' in mode) else fcntl.LOCK_SH
    open_kwargs = {}
    if encoding is not None:
        open_kwargs["encoding"] = encoding
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    actual_mode = mode
    truncate_after_lock = False
    if mode == 'w':
        if Path(path).exists():
            actual_mode = 'r+'
            truncate_after_lock = True
    try:
        f = open(path, actual_mode, **open_kwargs)
    except FileNotFoundError:
        if mode == 'w':
            f = open(path, 'w', **open_kwargs)
        else:
            raise
    with f:
        fcntl.flock(f, lock_type)
        if truncate_after_lock:
            f.seek(0)
            f.truncate()
        try:
            yield f
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def locked_file(path, mode='r', lock_type=None, encoding=None):
    """Open data only after acquiring its stable sidecar lock.

    Locking the replaceable data inode is unsafe: a waiter may open the old
    inode before an atomic writer replaces the path, then later acquire a lock
    that no longer serializes the live file.  Every reader, writer, appender and
    archival scanner therefore locks ``<path>.lock`` first and opens the data
    path only inside that critical section.
    """

    path = Path(path)
    if lock_type is None:
        lock_type = (
            fcntl.LOCK_EX
            if any(flag in mode for flag in ("w", "a", "x", "+"))
            else fcntl.LOCK_SH
        )
    normalized = mode.replace("b", "").replace("t", "")
    flags_by_mode = {
        "r": os.O_RDONLY,
        "r+": os.O_RDWR,
        # Truncating modes are published from a private inode below.  Never
        # put O_TRUNC on an open of the live path: a path swapped to a
        # hardlink after lstat() would otherwise damage the linked victim
        # before descriptor/path authenticity could be checked.
        "w": os.O_WRONLY | os.O_CREAT,
        "w+": os.O_RDWR | os.O_CREAT,
        "a": os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        "a+": os.O_RDWR | os.O_CREAT | os.O_APPEND,
        "x": os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        "x+": os.O_RDWR | os.O_CREAT | os.O_EXCL,
    }
    if normalized not in flags_by_mode:
        raise ValueError(f"unsupported locked_file mode: {mode}")
    creating = any(flag in normalized for flag in ("w", "a", "x"))
    if creating:
        _assert_safe_state_parent(path)
    with _locked_state_sidecar(path, lock_type=lock_type):
        existing = None
        if os.path.lexists(path):
            existing = os.lstat(path)
            if (
                not stat.S_ISREG(existing.st_mode)
                or stat.S_ISLNK(existing.st_mode)
                or existing.st_nlink != 1
            ):
                raise OSError("locked data path must be a single-link regular file")
        if normalized in {"w", "w+"}:
            temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            temp_flags = flags_by_mode[normalized] | os.O_EXCL
            temp_flags |= getattr(os, "O_CLOEXEC", 0)
            temp_flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = None
            temporary_identity = None
            try:
                descriptor = os.open(temp, temp_flags, 0o600)
                binary = "b" in mode
                open_kwargs = (
                    {} if binary or encoding is None else {"encoding": encoding}
                )
                with os.fdopen(descriptor, mode, **open_kwargs) as handle:
                    descriptor = None
                    opened = _assert_open_regular_path(
                        temp,
                        handle,
                        label="locked state temporary data",
                    )
                    try:
                        yield handle
                    finally:
                        finished = _assert_open_regular_path(
                            temp,
                            handle,
                            label="locked state temporary data",
                        )
                        if (
                            finished.st_dev,
                            finished.st_ino,
                        ) != (
                            opened.st_dev,
                            opened.st_ino,
                        ):
                            raise OSError(
                                "locked state temporary data inode changed"
                            )
                    handle.flush()
                    os.fsync(handle.fileno())
                    temporary_identity = os.fstat(handle.fileno())

                # Fail closed if a writer that ignored the sidecar changed the
                # destination while the private inode was being populated.
                # Even a last-moment race after this proof is harmless to an
                # external hardlink victim: os.replace() only removes the
                # destination directory entry and never writes its inode.
                if existing is None:
                    if os.path.lexists(path):
                        raise OSError(
                            "locked state data target appeared during atomic write"
                        )
                else:
                    try:
                        current = os.lstat(path)
                    except OSError as exc:
                        raise OSError(
                            "locked state data target changed during atomic write"
                        ) from exc
                    if (
                        not stat.S_ISREG(current.st_mode)
                        or stat.S_ISLNK(current.st_mode)
                        or current.st_nlink != 1
                        or (current.st_dev, current.st_ino)
                        != (existing.st_dev, existing.st_ino)
                    ):
                        raise OSError(
                            "locked state data target changed during atomic write"
                        )

                os.replace(temp, path)
                published = os.lstat(path)
                if (
                    temporary_identity is None
                    or not stat.S_ISREG(published.st_mode)
                    or stat.S_ISLNK(published.st_mode)
                    or published.st_nlink != 1
                    or (published.st_dev, published.st_ino)
                    != (temporary_identity.st_dev, temporary_identity.st_ino)
                    or published.st_size != temporary_identity.st_size
                ):
                    raise OSError(
                        "locked state publication did not retain the temporary inode"
                    )
                _fsync_directory(path.parent)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
            return
        flags = flags_by_mode[normalized] | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        binary = "b" in mode
        open_kwargs = {} if binary or encoding is None else {"encoding": encoding}
        with os.fdopen(descriptor, mode, **open_kwargs) as handle:
            opened = _assert_open_regular_path(
                path,
                handle,
                label="locked state data",
            )
            if opened.st_nlink != 1:
                raise OSError("locked state data must have one link")
            try:
                yield handle
            finally:
                finished = _assert_open_regular_path(
                    path,
                    handle,
                    label="locked state data",
                )
                if finished.st_nlink != 1:
                    raise OSError("locked state data link count changed")
                if lock_type == fcntl.LOCK_SH and (
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                ) != (
                    finished.st_size,
                    finished.st_mtime_ns,
                    finished.st_ctime_ns,
                ):
                    raise OSError("locked state data changed during shared read")


def _fsync_directory(path):
    """Durably publish a directory-entry mutation.

    File ``fsync`` plus ``os.replace`` is atomic for readers, but the rename or
    unlink is not power-loss durable until the containing directory is synced.
    Publication/checkpoint code deliberately lets an ``OSError`` escape here:
    claiming a durable state after a failed directory sync would be unsafe.
    """

    directory = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_regular_state_file_and_parent(path):
    """Re-prove a published state inode and its directory durability."""

    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        live = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(live.st_mode)
            or opened.st_nlink != 1
            or live.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (live.st_dev, live.st_ino)
        ):
            raise OSError("state durability target is unsafe")
        os.fsync(descriptor)
        live_after = os.lstat(path)
        if (
            live_after.st_nlink != 1
            or (live_after.st_dev, live_after.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            raise OSError("state durability target changed")
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _sidecar_lock_path(path):
    path = Path(path)
    return path.with_suffix(path.suffix + ".lock")


def _assert_safe_state_parent(path):
    """Reject state publication through a symlink/non-directory parent."""

    parent = Path(path).parent
    if not os.path.lexists(parent):
        parent.mkdir(parents=True, exist_ok=True)
    try:
        parent_stat = os.lstat(parent)
    except OSError as exc:
        raise OSError(
            f"state parent metadata unavailable: {type(exc).__name__}"
        ) from exc
    if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
        raise OSError("state parent must be a non-symlink directory")


def _preflight_state_sidecar(path):
    _assert_safe_state_parent(path)
    lock_path = _sidecar_lock_path(path)
    if os.path.lexists(lock_path):
        metadata = os.lstat(lock_path)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise OSError("state sidecar lock must be a non-symlink regular file")


def _assert_open_regular_path(path, handle, *, label):
    """Bind an opened descriptor to the still-live regular-file path."""

    try:
        path_stat = os.lstat(path)
        file_stat = os.fstat(handle.fileno())
    except OSError as exc:
        raise OSError(
            f"{label} metadata unavailable: {type(exc).__name__}"
        ) from exc
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or not stat.S_ISREG(file_stat.st_mode)
        or path_stat.st_nlink != 1
        or file_stat.st_nlink != 1
        or path_stat.st_dev != file_stat.st_dev
        or path_stat.st_ino != file_stat.st_ino
    ):
        raise OSError(f"{label} path is not the opened safe regular file")
    return file_stat


@contextmanager
def _locked_state_sidecar(path, *, lock_type):
    """Lock a stable, no-follow sidecar inode shared by readers and writers."""

    path = Path(path)
    lock_path = _sidecar_lock_path(path)
    _preflight_state_sidecar(path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    with _thread_lock_for(lock_path):
        held_map = getattr(_STATE_SIDECAR_LOCAL, "held", None)
        if held_map is None:
            held_map = {}
            _STATE_SIDECAR_LOCAL.held = held_map
        lock_key = str(lock_path.resolve())
        held = held_map.get(lock_key)
        if held is not None:
            # Re-entering an exclusive lock as EX or SH is safe and must not
            # open/flock a second descriptor: several publication transactions
            # deliberately call checkpoint readers while owning the checkpoint
            # CAS lock.  A SH -> EX upgrade is rejected instead of deadlocking
            # or silently weakening the outer reader lease.
            if held["lock_type"] != fcntl.LOCK_EX and lock_type == fcntl.LOCK_EX:
                raise OSError("state sidecar shared lock cannot be upgraded")
            held["depth"] += 1
            integrity_error = None
            try:
                _assert_open_regular_path(
                    lock_path,
                    held["handle"],
                    label="state sidecar lock",
                )
                yield held["handle"]
            finally:
                try:
                    _assert_open_regular_path(
                        lock_path,
                        held["handle"],
                        label="state sidecar lock",
                    )
                except BaseException as exc:
                    integrity_error = exc
                held["depth"] -= 1
                if integrity_error is not None:
                    raise integrity_error
            return
        descriptor = os.open(lock_path, flags, 0o600)
        with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle, lock_type)
            integrity_error = None
            try:
                _assert_open_regular_path(
                    lock_path,
                    handle,
                    label="state sidecar lock",
                )
                held_map[lock_key] = {
                    "handle": handle,
                    "lock_type": lock_type,
                    "depth": 1,
                }
                yield handle
            finally:
                # Run the exit proof even when the protected body raises.  A
                # body failure must not hide that the lock inode was swapped
                # while the supposedly serialized effect was in flight.
                try:
                    _assert_open_regular_path(
                        lock_path,
                        handle,
                        label="state sidecar lock",
                    )
                except BaseException as exc:
                    integrity_error = exc
                held_map.pop(lock_key, None)
                fcntl.flock(handle, fcntl.LOCK_UN)
                if integrity_error is not None:
                    raise integrity_error


@contextmanager
def bot_publication_lock(*, results_dir=None):
    """Lock the one stable no-follow publication/cleanup linearization inode."""

    root = Path(results_dir) if results_dir is not None else Path(RESULTS_DIR)
    lock_path = root / ".bot_publication.lock"
    _assert_safe_state_parent(lock_path)
    if os.path.lexists(lock_path):
        metadata = os.lstat(lock_path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise OSError(
                "bot publication lock must be a single-link regular file"
            )
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    with _thread_lock_for(lock_path):
        descriptor = os.open(lock_path, flags, 0o600)
        with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
            opened = os.fstat(handle.fileno())
            fcntl.flock(handle, fcntl.LOCK_EX)
            integrity_error = None
            try:
                live = os.lstat(lock_path)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or not stat.S_ISREG(live.st_mode)
                    or opened.st_nlink != 1
                    or live.st_nlink != 1
                    or (opened.st_dev, opened.st_ino)
                    != (live.st_dev, live.st_ino)
                ):
                    raise OSError("bot publication lock path is unsafe")
                yield handle
            finally:
                try:
                    live_after = os.lstat(lock_path)
                    opened_after = os.fstat(handle.fileno())
                    if (
                        opened_after.st_nlink != 1
                        or live_after.st_nlink != 1
                        or (opened_after.st_dev, opened_after.st_ino)
                        != (opened.st_dev, opened.st_ino)
                        or (live_after.st_dev, live_after.st_ino)
                        != (opened.st_dev, opened.st_ino)
                    ):
                        raise OSError("bot publication lock inode changed")
                except BaseException as exc:
                    integrity_error = exc
                fcntl.flock(handle, fcntl.LOCK_UN)
                if integrity_error is not None:
                    raise integrity_error


def _read_regular_state_text(path, *, allow_missing):
    """Read one state file without following links and revalidate after read."""

    path = Path(path)
    if not os.path.lexists(path):
        if allow_missing:
            return ""
        raise FileNotFoundError(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        _assert_open_regular_path(path, handle, label="state data")
        raw = handle.read()
        _assert_open_regular_path(path, handle, label="state data")
    return raw


def _atomic_publish_state_text(path, raw):
    """Publish complete UTF-8 state bytes atomically; caller owns sidecar EX."""

    path = Path(path)
    _assert_safe_state_parent(path)
    if os.path.lexists(path):
        path_stat = os.lstat(path)
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or path_stat.st_nlink != 1
        ):
            raise OSError(
                "state data target must be a single-link non-symlink regular file"
            )
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    temporary_identity = None
    try:
        descriptor = os.open(temp, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_identity = os.fstat(handle.fileno())
        os.replace(temp, path)
        published_stat = os.lstat(path)
        if (
            temporary_identity is None
            or not stat.S_ISREG(published_stat.st_mode)
            or stat.S_ISLNK(published_stat.st_mode)
            or published_stat.st_nlink != 1
            or (published_stat.st_dev, published_stat.st_ino)
            != (temporary_identity.st_dev, temporary_identity.st_ino)
            or published_stat.st_size != temporary_identity.st_size
        ):
            raise OSError(
                "atomic state publication did not retain the temporary inode"
            )
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def read_locked_json(path, default=None):
    """Read a JSON file with shared lock. Returns default on any error."""
    try:
        with locked_file(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return default


def read_and_maybe_unlink_locked_text(path, should_unlink):
    """Read and conditionally consume one state inode under its EX sidecar.

    The predicate runs while the stable sidecar is exclusively held.  When it
    returns true, this function re-proves that the live path is still the exact
    no-follow, single-link inode that was read before unlinking it and syncing
    the containing directory.  A cooperating atomic writer therefore runs
    wholly before the read or wholly after the durable unlink; a later write is
    never mistaken for the inode selected for consumption.

    Return ``(raw_text, consumed)``.  A missing path is not an error and returns
    ``(None, False)``.  Predicate, authenticity, unlink, and durability errors
    are fail-closed and propagate to the caller.
    """

    if not callable(should_unlink):
        raise TypeError("state consumption predicate must be callable")
    path = Path(path)
    with _locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return None, False

        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            opened = _assert_open_regular_path(
                path,
                handle,
                label="consumable state data",
            )
            raw = handle.read()
            finished = _assert_open_regular_path(
                path,
                handle,
                label="consumable state data",
            )
            if (
                (finished.st_dev, finished.st_ino)
                != (opened.st_dev, opened.st_ino)
                or (
                    finished.st_size,
                    finished.st_mtime_ns,
                    finished.st_ctime_ns,
                )
                != (
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
            ):
                raise OSError("consumable state data changed during read")

            consume = bool(should_unlink(raw))
            if not consume:
                return raw, False

            # The decision and this final path/inode proof share one EX
            # sidecar lease.  In particular, never perform a path-only unlink
            # after releasing the lock: an atomic writer may have installed a
            # new inode by then.
            current = _assert_open_regular_path(
                path,
                handle,
                label="consumable state data",
            )
            if (
                (current.st_dev, current.st_ino)
                != (opened.st_dev, opened.st_ino)
                or (
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_ctime_ns,
                )
                != (
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
            ):
                raise OSError("consumable state data changed before unlink")

            os.unlink(path)
            post_unlink_error = None
            try:
                retired = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(retired.st_mode)
                    or (retired.st_dev, retired.st_ino)
                    != (opened.st_dev, opened.st_ino)
                    or retired.st_nlink != 0
                ):
                    raise OSError("consumed state inode retirement is unsafe")

                # An uncooperative writer may create a later inode without the
                # sidecar.  Do not remove it: only prove that it is distinct
                # from the inode selected above.  Cooperating writers cannot
                # reach this point until the sidecar is released.
                if os.path.lexists(path):
                    replacement = os.lstat(path)
                    if (
                        not stat.S_ISREG(replacement.st_mode)
                        or stat.S_ISLNK(replacement.st_mode)
                        or replacement.st_nlink != 1
                        or (replacement.st_dev, replacement.st_ino)
                        == (opened.st_dev, opened.st_ino)
                    ):
                        raise OSError(
                            "replacement state path after consumption is unsafe"
                        )
            except BaseException as exc:
                post_unlink_error = exc

            try:
                _fsync_directory(path.parent)
            except BaseException as sync_exc:
                if post_unlink_error is not None:
                    raise post_unlink_error from sync_exc
                raise
            if post_unlink_error is not None:
                raise post_unlink_error
            return raw, True


def write_locked_json(path, data, indent=2):
    """Atomically and durably publish JSON under the stable sidecar lock."""
    path = Path(path)
    raw = json.dumps(
        data,
        indent=indent,
        ensure_ascii=False,
        allow_nan=False,
    )
    with _locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
        _atomic_publish_state_text(path, raw)


def append_locked_jsonl(path, entry):
    """Durably append one JSON row under the same stable sidecar lock."""
    path = Path(path)
    raw = json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n"
    with _locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
        existed = os.path.lexists(path)
        if existed:
            metadata = os.lstat(path)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise OSError("JSONL append target is unsafe")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            opened = os.fstat(descriptor)
            live = os.lstat(path)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or live.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (live.st_dev, live.st_ino)
            ):
                raise OSError("JSONL append target identity changed")
            encoded = raw.encode("utf-8")
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise OSError("JSONL append made no progress")
                offset += written
            os.fsync(descriptor)
            opened_after = os.fstat(descriptor)
            live_after = os.lstat(path)
            if (
                opened_after.st_nlink != 1
                or live_after.st_nlink != 1
                or (opened_after.st_dev, opened_after.st_ino)
                != (opened.st_dev, opened.st_ino)
                or (live_after.st_dev, live_after.st_ino)
                != (opened.st_dev, opened.st_ino)
            ):
                raise OSError("JSONL append target changed during write")
        finally:
            os.close(descriptor)
        if not existed:
            _fsync_directory(path.parent)


def update_h2h(h2h, bot_a, bot_b, wins_a, wins_b, draws=0):
    """Update H2H dict for a pair of bots.

    Accepts either one-game increments or aggregated match totals. Older callers
    use it per game; manual inline evaluation passes a whole mirror-battle
    summary, so games must advance by wins + losses + draws, not by one call.
    """
    key = pair_key(bot_a, bot_b)
    entry = h2h.setdefault(key, {"games": 0, "a_wins": 0, "b_wins": 0, "draws": 0})
    total = int(wins_a or 0) + int(wins_b or 0) + int(draws or 0)
    if total <= 0:
        return
    entry["games"] += total
    if bot_a < bot_b:
        entry["a_wins"] += wins_a
        entry["b_wins"] += wins_b
    else:
        entry["a_wins"] += wins_b
        entry["b_wins"] += wins_a
    entry["draws"] += draws
    entry["win_rate"] = round((entry["a_wins"] + 0.5 * entry["draws"]) / entry["games"], 4)


def update_bot_stats(bot_stats, name, wins, losses, draws=0):
    """Update per-bot stats dict. Creates entry if not present."""
    entry = bot_stats.setdefault(name, {"wins": 0, "losses": 0, "draws": 0, "games": 0, "win_rate": 0.0})
    entry["wins"] += wins
    entry["losses"] += losses
    entry["draws"] += draws
    entry["games"] += wins + losses + draws
    if entry["games"] > 0:
        entry["win_rate"] = round(
            (entry["wins"] + 0.5 * entry["draws"]) / entry["games"],
            4,
        )


def substitute_template(template, replacements):
    """Replace {key} placeholders in a template string. Warns on unreplaced placeholders."""
    result = template
    for key, value in replacements.items():
        result = result.replace(f"{{{key}}}", str(value))
    remaining = set(re.findall(r'\{([a-z_]+)\}', result))
    if remaining:
        # root-cause-audit 2026-06-21: worker_prompt 等 template 含 Python f-string 示例代码，
        # {d}/{n_calls}/{bp}/{cr} 等是合法代码变量非模板占位符，贪婪正则误报(曾 268 WARNING/
        # 周期，worker 实际收到完整正确代码)。降为 debug：开发期开 DEBUG 可见真未替换占位符，
        # 生产日志静默。
        log.debug("Unreplaced template placeholders (likely f-string code vars): %s", remaining)
    return result


# ──────────────────────────────────────────────
# Pipeline Checkpoint (Process Recovery)
# ──────────────────────────────────────────────

def _capture_repo_baseline(stage, *, next_v=None, source_v=None, checkpoint=None):
    """Capture the git baseline persisted with an active generation checkpoint."""
    try:
        from repo_state import git_worktree_snapshot
        snapshot = git_worktree_snapshot()
        contract = build_evaluation_contract(
            PROJECT_ROOT,
            candidate_v=next_v,
            source_v=source_v,
            checkpoint=checkpoint,
            stage=stage,
            include_hash=True,
        )
        return {
            "branch": snapshot.get("branch", ""),
            "head": snapshot.get("head", ""),
            "entry_count": snapshot.get("entry_count", 0),
            "dirty_count": snapshot.get("dirty_count", 0),
            "untracked_count": snapshot.get("untracked_count", 0),
            "entries": (snapshot.get("entries") or [])[:40],
            "truncated": bool(snapshot.get("truncated")),
            "evaluation_contract": contract,
            "captured_stage": stage,
            "captured_ts": time.time(),
        }
    except Exception as exc:
        return {
            "branch": "",
            "head": "",
            "entry_count": 0,
            "dirty_count": 0,
            "untracked_count": 0,
            "entries": [],
            "truncated": False,
            "evaluation_contract": {},
            "captured_stage": stage,
            "captured_ts": time.time(),
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


_REPO_BASELINE_VALIDATION_STAGES = frozenset({
    "quality_failed",
    "quality_passed",
    "precommit_failed",
    "verified",
    "official_bootstrap_required",
    "official_certifying",
    "official_failed",
    "official_inconclusive",
})
_REPO_BASELINE_VALIDATION_GATES = {
    "quality_failed": "quality",
    "quality_passed": "quality",
    "precommit_failed": "precommit_eval",
    "verified": "precommit_eval",
    "official_bootstrap_required": "official_full",
    "official_certifying": "official_full",
    "official_failed": "official_full",
    "official_inconclusive": "official_full",
}
_REPO_BASELINE_PLANNING_STAGES = frozenset({
    "direction_audited",
    "master_planned",
})


def _prune_gate_results_for_stage(stage, gate_results):
    """Drop gate results that no longer validate the current stage/code.

    Gate payloads are evidence for a specific code snapshot. When the pipeline
    regresses to a code-mutating stage and later returns to workers_done, old
    review/critic/precommit evidence must not survive and steer recovery for the
    new code.
    """
    if not isinstance(gate_results, dict):
        return {}
    allowed = STAGE_GATE_ALLOWLIST.get(stage)
    if allowed is None:
        return dict(gate_results)
    return {name: value for name, value in gate_results.items() if name in allowed}


def _stage_refreshes_repo_baseline(old_stage, new_stage, gate_results=None) -> bool:
    """Return True when a checkpoint stage proves the candidate on this HEAD.

    HEAD-drift recovery can legitimately route a candidate through a hard gate
    after infrastructure changes. Once that gate finishes, the persisted
    baseline must move forward to the HEAD that actually ran the validation;
    otherwise later recovery health checks keep comparing against stale code.

    Pre-worker planning stages also refresh the baseline on stage advance. They
    do not validate candidate strength, but they do bind the next deterministic
    handoff to prompts, guard policy, and route logic from the current HEAD.
    """
    if old_stage != new_stage and new_stage in _REPO_BASELINE_PLANNING_STAGES:
        return True
    if new_stage not in _REPO_BASELINE_VALIDATION_STAGES:
        return False
    if old_stage != new_stage:
        return True
    required_gate = _REPO_BASELINE_VALIDATION_GATES.get(new_stage)
    return bool(required_gate and required_gate in (gate_results or {}))


def _publication_checkpoint_reconciliation_allowed(checkpoint, authority):
    """Recognize only the fully proven intent-bound publication window.

    A missing ``.completed`` is allowed solely because creating that sentinel is
    itself one of the remaining idempotent recovery effects.  If it exists, its
    publication id must match exactly.  Lightweight tags, one-tag-only state,
    wrong-tree refs, invalid certificates, or a mismatched sentinel all fail.
    """

    if not isinstance(checkpoint, dict) or checkpoint.get("stage") != "publishing":
        return False
    target = checkpoint.get("next_v")
    if (
        type(target) is not int
        or int(authority.get("published_high_water") or 0) != target
    ):
        return False
    intent = checkpoint.get("publication_intent")
    try:
        from publication_transaction import publication_intent_structure_errors

        if publication_intent_structure_errors(intent):
            return False
    except Exception:
        return False
    if (
        intent.get("version") != target
        or intent.get("workflow_run_id") != checkpoint.get("workflow_run_id")
        or intent.get("completion_tag") != bot_tag(target)
    ):
        return False
    try:
        commit_oid = _git(
            "rev-parse",
            f"refs/tags/{intent['completion_tag']}^{{commit}}",
            check=False,
        ).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", commit_oid):
            return False
        _validate_local_publication_refs(intent, commit_oid)
        _validate_existing_publication_commit(intent, commit_oid)

        from national_runtime_authority import build_pending_local_publication_proof

        bot_dir = get_bot_dir(target)
        proof = build_pending_local_publication_proof(bot_dir)
        if (
            proof.get("version") != target
            or proof.get("artifact_hash") != intent.get("candidate_artifact_hash")
            or proof.get("commit_oid") != commit_oid
            or proof.get("tag") != intent.get("completion_tag")
        ):
            return False
        spec = resolve_national_bot_spec(
            bot_dir,
            role=ROLE_PARENT_SOURCE,
            repo_root=PROJECT_ROOT,
        )
        if (
            not spec.eligible
            or spec.certificate_digest
            != intent.get("official_certificate_digest")
        ):
            return False
        sentinel = bot_dir / ".completed"
        if os.path.lexists(sentinel):
            metadata = os.lstat(sentinel)
            if not stat.S_ISREG(metadata.st_mode) or sentinel.is_symlink():
                return False
            if sentinel.read_text(encoding="utf-8") != (
                f"publication_id={intent.get('publication_id')}\n"
            ):
                return False
        return True
    except Exception:
        return False


def partial_publication_checkpoint_recovery_allowed(
    checkpoint,
    *,
    namespace_authority,
):
    """Prove the sole one-ref publication crash window without reallocating it."""

    if not isinstance(checkpoint, dict) or checkpoint.get("stage") != "publishing":
        return False
    paired = int(getattr(namespace_authority, "high_water", 0) or 0)
    completion_only = set(
        getattr(namespace_authority, "unpaired_completion_versions", ()) or ()
    )
    high_water_only = set(
        getattr(namespace_authority, "unpaired_high_water_versions", ()) or ()
    )
    occupied = completion_only | high_water_only
    target = checkpoint.get("next_v")
    if (
        type(target) is not int
        or target != paired + 1
        or occupied != {target}
        or target in completion_only.intersection(high_water_only)
    ):
        return False
    try:
        from bot_artifact import hash_path
        from checkpoint_schema import (
            checkpoint_epoch_errors,
            live_checkpoint_allocation_authority_errors,
            live_checkpoint_parent_authority_errors,
            live_policy_epoch_reset_receipt_errors,
        )
        from publication_transaction import publication_intent_structure_errors

        if checkpoint_epoch_errors(checkpoint):
            return False
        if live_checkpoint_parent_authority_errors(
            checkpoint,
            repo_root=PROJECT_ROOT,
        ):
            return False
        if live_policy_epoch_reset_receipt_errors(
            checkpoint,
            project_root=PROJECT_ROOT,
        ):
            return False
        receipts = load_abandoned_version_receipts(
            project_root=PROJECT_ROOT,
        )
        abandon_authority = _abandon_authority_from_receipts(
            receipts,
            published_high_water=paired,
            retryable_first_strict=(paired < FIRST_STRICT_POLICY_VERSION),
        )
        if target != max(paired, int(abandon_authority["floor"])) + 1:
            return False
        if live_checkpoint_allocation_authority_errors(
            checkpoint,
            published_high_water=paired,
            abandoned_receipt_floor=int(abandon_authority["floor"]),
            abandoned_receipt_head_digest=abandon_authority["head_digest"],
        ):
            return False
        binding = checkpoint.get("epoch_binding") or {}
        if binding.get("published_high_water") != paired:
            return False
        intent = checkpoint.get("publication_intent")
        if publication_intent_structure_errors(intent):
            return False
        if (
            intent.get("version") != target
            or intent.get("workflow_run_id") != checkpoint.get("workflow_run_id")
            or intent.get("completion_tag") != bot_tag(target)
            or intent.get("high_water_tag") != f"national-high-water-v{target}"
        ):
            return False
        present_tag = (
            intent["completion_tag"]
            if target in completion_only
            else intent["high_water_tag"]
        )
        ref = f"refs/tags/{present_tag}"
        if _git("cat-file", "-t", ref, check=False).strip() != "tag":
            return False
        commit_oid = _git(
            "rev-parse",
            f"{ref}^{{commit}}",
            check=False,
        ).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", commit_oid):
            return False
        _validate_existing_publication_commit(intent, commit_oid)
        _validate_publication_certificate_file(intent)
        if hash_path(get_bot_dir(target)) != intent.get("candidate_artifact_hash"):
            return False
        return True
    except Exception:
        return False


_IDENTITY_REPLAN_PREPARED_CONTRACT_FIELDS = frozenset({
    "schema_version",
    "source_v",
    "next_v",
    "prepared_bot",
    "prepared_artifact_hash",
    "prepared_artifact_manifest",
    "contract_digest",
})
_IDENTITY_REPLAN_RECEIPT_FIELDS = frozenset({
    "schema_version",
    "kind",
    "source_v",
    "next_v",
    "workflow_run_id",
    "checkpoint_preimage_revision",
    "checkpoint_preimage_stage",
    "source_stage",
    "recovery_mode",
    "identity_errors",
    "source_artifact_hash",
    "replaced_artifact_hash",
    "prepared_artifact_hash",
    "prepared_artifact_contract_digest",
    "runtime_manifest_digest",
    "epoch_receipt_digest",
    "runtime_manifest_file_sha256",
    "epoch_receipt_file_sha256",
    "materialization_operation_id",
    "materialization_expected_destination_digest",
    "materialization_receipt_digest",
    "candidate_reset_to_source",
    "target_identity_refreshed",
    "stale_worker_gate_identity_cleared",
    "receipt_digest",
})
_IDENTITY_REPLAN_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


def _identity_replan_replacement_contract_errors(
    *,
    replacement,
    next_v,
    source_v,
    workflow_run_id,
    checkpoint_revision,
    checkpoint_stage,
    epoch_binding,
):
    """Validate the closed, subject-bound destructive replan contract.

    A canonical digest proves integrity only; it does not grant mutation
    authority.  This validator binds the replacement to the exact checkpoint
    CAS subject, parent publication identity, prepared manifest, and journaled
    materialization shape before any durable field may be cleared.
    """

    from bot_artifact import canonical_digest

    errors = []
    if not isinstance(replacement, dict):
        return ["identity_replan_replacement_not_object"]
    prepared = replacement.get("prepared_artifact_contract")
    replan = replacement.get("architecture_policy_identity_replan")
    if not isinstance(prepared, dict):
        errors.append("identity_replan_prepared_contract_not_object")
        prepared = {}
    if not isinstance(replan, dict):
        errors.append("identity_replan_receipt_not_object")
        replan = {}
    if set(prepared) != _IDENTITY_REPLAN_PREPARED_CONTRACT_FIELDS:
        errors.append("identity_replan_prepared_contract_fields_mismatch")
    if set(replan) != _IDENTITY_REPLAN_RECEIPT_FIELDS:
        errors.append("identity_replan_receipt_fields_mismatch")

    digest_re = re.compile(r"^[0-9a-f]{64}$")
    prepared_manifest = prepared.get("prepared_artifact_manifest")
    prepared_hash = str(prepared.get("prepared_artifact_hash") or "")
    if prepared.get("schema_version") != 1:
        errors.append("identity_replan_prepared_schema_mismatch")
    if prepared.get("source_v") != int(source_v):
        errors.append("identity_replan_prepared_source_mismatch")
    if prepared.get("next_v") != int(next_v):
        errors.append("identity_replan_prepared_target_mismatch")
    if prepared.get("prepared_bot") != bot_name(next_v):
        errors.append("identity_replan_prepared_bot_mismatch")
    if not digest_re.fullmatch(prepared_hash):
        errors.append("identity_replan_prepared_hash_invalid")
    if (
        not isinstance(prepared_manifest, dict)
        or set(prepared_manifest) != {"schema_version", "artifact_type", "entries"}
        or prepared_manifest.get("artifact_type") != "directory"
        or not isinstance(prepared_manifest.get("entries"), list)
    ):
        errors.append("identity_replan_prepared_manifest_invalid")
    elif prepared_hash != canonical_digest(prepared_manifest):
        errors.append("identity_replan_prepared_manifest_hash_mismatch")
    prepared_unsigned = {
        key: value for key, value in prepared.items() if key != "contract_digest"
    }
    if prepared.get("contract_digest") != canonical_digest(prepared_unsigned):
        errors.append("identity_replan_prepared_contract_digest_mismatch")

    if replan.get("schema_version") != 2:
        errors.append("identity_replan_receipt_schema_mismatch")
    if replan.get("kind") != "single-parent-architecture-policy-identity-replan-v2":
        errors.append("identity_replan_receipt_kind_mismatch")
    if replan.get("source_v") != int(source_v):
        errors.append("identity_replan_receipt_source_mismatch")
    if replan.get("next_v") != int(next_v):
        errors.append("identity_replan_receipt_target_mismatch")
    if replan.get("workflow_run_id") != str(workflow_run_id):
        errors.append("identity_replan_receipt_workflow_mismatch")
    if replan.get("checkpoint_preimage_revision") != int(checkpoint_revision):
        errors.append("identity_replan_receipt_revision_mismatch")
    if replan.get("checkpoint_preimage_stage") != str(checkpoint_stage):
        errors.append("identity_replan_receipt_stage_mismatch")
    if replan.get("source_stage") not in {
        "quality_failed",
        "repair_planned",
        "rework_running",
    }:
        errors.append("identity_replan_receipt_source_stage_invalid")
    expected_mode = (
        "legacy_parent_copy_recovery"
        if checkpoint_stage == "direction_audited"
        else "quality_identity_replan"
    )
    if replan.get("recovery_mode") != expected_mode:
        errors.append("identity_replan_receipt_recovery_mode_mismatch")
    identity_errors = replan.get("identity_errors")
    if (
        not isinstance(identity_errors, list)
        or not identity_errors
        or any(not isinstance(item, str) or not item for item in identity_errors)
    ):
        errors.append("identity_replan_receipt_identity_errors_invalid")
    for field in (
        "source_artifact_hash",
        "replaced_artifact_hash",
        "prepared_artifact_hash",
        "prepared_artifact_contract_digest",
        "runtime_manifest_digest",
        "epoch_receipt_digest",
        "runtime_manifest_file_sha256",
        "epoch_receipt_file_sha256",
        "materialization_expected_destination_digest",
        "materialization_receipt_digest",
    ):
        if not digest_re.fullmatch(str(replan.get(field) or "")):
            errors.append(f"identity_replan_receipt_{field}_invalid")
    if not _IDENTITY_REPLAN_OPERATION_ID_RE.fullmatch(
        str(replan.get("materialization_operation_id") or "")
    ):
        errors.append("identity_replan_receipt_operation_id_invalid")
    if replan.get("materialization_expected_destination_digest") != replan.get(
        "replaced_artifact_hash"
    ):
        errors.append("identity_replan_materialization_preimage_mismatch")
    if replan.get("prepared_artifact_hash") != prepared_hash:
        errors.append("identity_replan_receipt_prepared_hash_mismatch")
    if replan.get("prepared_artifact_contract_digest") != prepared.get(
        "contract_digest"
    ):
        errors.append("identity_replan_receipt_prepared_contract_mismatch")
    for field in (
        "candidate_reset_to_source",
        "target_identity_refreshed",
        "stale_worker_gate_identity_cleared",
    ):
        if replan.get(field) is not True:
            errors.append(f"identity_replan_receipt_{field}_not_true")

    entries = (
        prepared_manifest.get("entries")
        if isinstance(prepared_manifest, dict)
        else []
    )
    files = {
        str(item.get("path") or ""): str(item.get("sha256") or "")
        for item in entries or []
        if isinstance(item, dict) and item.get("type") == "file"
    }
    if files.get("national_runtime_manifest.json") != replan.get(
        "runtime_manifest_file_sha256"
    ):
        errors.append("identity_replan_runtime_manifest_binding_mismatch")
    if files.get("policy_epoch_receipt.json") != replan.get(
        "epoch_receipt_file_sha256"
    ):
        errors.append("identity_replan_epoch_receipt_binding_mismatch")

    parent_identities = (
        (epoch_binding or {}).get("published_parent_identities") or []
    )
    source_bindings = [
        item
        for item in parent_identities
        if isinstance(item, dict) and item.get("version") == int(source_v)
    ]
    if (
        len(source_bindings) != 1
        or source_bindings[0].get("tag_artifact_hash")
        != replan.get("source_artifact_hash")
    ):
        errors.append("identity_replan_source_publication_binding_mismatch")

    replan_unsigned = {
        key: value for key, value in replan.items() if key != "receipt_digest"
    }
    if replan.get("receipt_digest") != canonical_digest(replan_unsigned):
        errors.append("identity_replan_receipt_digest_mismatch")
    return list(dict.fromkeys(errors))


def _identity_replan_live_materialization_errors(
    replacement,
    *,
    candidate_dir=None,
    artifact_root=None,
):
    """Cross-bind one closed receipt to live bytes and durable CAS evidence."""

    from bot_artifact import hash_path
    from worker_workflow import WorkerArtifactStore

    if not isinstance(replacement, dict):
        return ["identity_replan_replacement_not_object"]
    prepared = replacement.get("prepared_artifact_contract") or {}
    replan = replacement.get("architecture_policy_identity_replan") or {}
    next_v = replan.get("next_v")
    try:
        target = (
            Path(candidate_dir)
            if candidate_dir is not None
            else BOTS_DIR / bot_name(int(next_v))
        )
        prepared_hash = str(prepared.get("prepared_artifact_hash") or "")
        if hash_path(target) != prepared_hash:
            return ["identity_replan_live_candidate_hash_mismatch"]
        WorkerArtifactStore(
            Path(artifact_root)
            if artifact_root is not None
            else RESULTS_DIR / "workflow" / "artifacts"
        ).verify_materialization_receipt(
            str(replan.get("materialization_operation_id") or ""),
            destination=target,
            digest=prepared_hash,
            expected_destination_digest=str(
                replan.get("materialization_expected_destination_digest") or ""
            ),
            receipt_digest=str(
                replan.get("materialization_receipt_digest") or ""
            ),
        )
    except Exception as exc:
        return [
            "identity_replan_materialization_receipt_invalid:"
            + type(exc).__name__
        ]
    return []


def write_pipeline_checkpoint(next_v, source_v, stage, master_plan=None,
                               reviewer_feedback="", generation_attempt=0,
                               gate_results=None, worker_failure_count=None,
                               worker_invocation_count=None,
                               parent2_v=None, direction_audit=None,
                               audit_context=None, reset_generation_attempt=False,
                               replace_audit_context=False,
                               audit_context_replacement_reason=None,
                               audit_attempt=None, reset_audit_attempt=False,
                               precommit_attempt=None, reset_precommit_attempt=False,
                               precommit_rework_count=None,
                               official_rework_count=None,
                               timeout_extensions=None, touch_stage_timestamp=False,
                               literature_probe=None, prepare_scope_files=None,
                               clear_reviewer_feedback=False,
                               infra_failure=None, clear_infra_failure=False,
                               infra_failure_owner=None,
                               expected_infra_failure_digest=None,
                               official_job=None, clear_official_job=False,
                               expected_official_job_id=None,
                               repair_baseline_artifact_hash=None,
                               clear_repair_baseline_artifact_hash=False,
                               reset_runtime_contract_ledger=False,
                               expected_runtime_contract_ledger_digest=None,
                               runtime_contract_ledger_reset_reason=None,
                               publication_intent=None,
                               expected_checkpoint_revision=None,
                               expected_checkpoint_stage=None,
                               expected_workflow_run_id=None,
                               workflow_run_id=None,
                               terminal_gate_outcome=None,
                               review_attempt_journal=None,
                               identity_replan_history=None):
    """Write pipeline stage checkpoint so a killed process can resume.

    Uses atomic tmp+rename under exclusive lock to prevent concurrent
    read-merge-write races (POSIX guarantees os.replace is atomic). Runtime
    contract ledgers remain append-only unless a state-machine-authorized plan
    rejection supplies an explicit reset reason and expected ledger digest.
    """
    from workflow_profiles import get_workflow_profile

    _profile = get_workflow_profile()
    current_workflow_profile_id = getattr(_profile, "profile_id", "")
    current_national_execution_mode = getattr(
        _profile, "national_execution_mode", "native_tcp"
    )

    # Lock a stable sidecar inode. Locking PIPELINE_STATE_FILE itself is unsafe
    # because os.replace swaps that inode while waiters may still hold an open
    # descriptor to the retired file and later overwrite a newer projection.
    try:
        _preflight_state_sidecar(PIPELINE_STATE_FILE)
    except OSError as exc:
        log.error("Checkpoint sidecar path is unsafe: %s", exc)
        return False
    with _locked_state_sidecar(PIPELINE_STATE_FILE, lock_type=fcntl.LOCK_EX):
        try:
            raw = _read_regular_state_text(
                PIPELINE_STATE_FILE,
                allow_missing=True,
            )
        except (OSError, UnicodeError) as exc:
            log.error("Checkpoint path is unsafe or unreadable: %s", exc)
            return False
        existing = None
        if raw.strip():
            try:
                existing = json.loads(raw)
            except Exception as exc:
                log.error(
                    "Refusing to overwrite non-empty corrupt pipeline checkpoint: %s",
                    exc,
                )
                return False
            if not isinstance(existing, dict):
                log.error("Refusing non-object pipeline checkpoint")
                return False
        try:
            allocation_authority = checkpoint_allocation_authority(
                expected_next_v=next_v,
            )
        except Exception as exc:
            log.error(
                "Checkpoint allocation authority is unavailable: %s: %s",
                type(exc).__name__,
                exc,
            )
            return False
        if isinstance(existing, dict):
            try:
                from pipeline_infrastructure import normalize_checkpoint_infrastructure

                existing = normalize_checkpoint_infrastructure(existing)
            except Exception as exc:
                log.error("Checkpoint infrastructure normalization failed closed: %s", exc)
                return False
            active_stage = existing.get("stage")
            if active_stage not in {
                None,
                "archived",
                "abandoned",
            }:
                from checkpoint_schema import (
                    checkpoint_epoch_errors,
                    live_checkpoint_allocation_authority_errors,
                    live_checkpoint_parent_authority_errors,
                )

                epoch_errors = checkpoint_epoch_errors(existing)
                if not epoch_errors:
                    epoch_errors.extend(
                        live_checkpoint_parent_authority_errors(
                            existing,
                            repo_root=PROJECT_ROOT,
                        )
                    )
                if not epoch_errors:
                    epoch_errors.extend(
                        live_checkpoint_allocation_authority_errors(
                            existing,
                            published_high_water=allocation_authority[
                                "published_high_water"
                            ],
                            abandoned_receipt_floor=allocation_authority[
                                "abandoned_receipt_floor"
                            ],
                            abandoned_receipt_head_digest=allocation_authority[
                                "abandoned_receipt_head_digest"
                            ],
                            allow_published_target_reconciliation=(
                                _publication_checkpoint_reconciliation_allowed(
                                    existing,
                                    allocation_authority,
                                )
                            ),
                        )
                    )
                if epoch_errors:
                    log.error(
                        "Refusing implicit epoch/schema upgrade of active "
                        "checkpoint at %s; operator archive/reset is required: %s",
                        active_stage,
                        epoch_errors,
                    )
                    return False
            try:
                active_revision = int(existing.get("checkpoint_revision") or 0)
            except (TypeError, ValueError):
                active_revision = -1
            if active_stage not in {
                None,
                "archived",
                "abandoned",
            } and (
                not str(existing.get("workflow_run_id") or "").strip()
                or active_revision < 1
            ):
                log.error(
                    "Refusing implicit upgrade of active legacy checkpoint at %s; "
                    "central abandon is required",
                    active_stage,
                )
                return False

        if expected_checkpoint_revision is not None:
            current_revision = (
                int(existing.get("checkpoint_revision") or 0)
                if isinstance(existing, dict)
                else 0
            )
            if current_revision != int(expected_checkpoint_revision):
                log.warning(
                    "Checkpoint revision compare-and-swap rejected: expected=%s current=%s",
                    expected_checkpoint_revision,
                    current_revision,
                )
                return False
        if expected_checkpoint_stage is not None and (
            not isinstance(existing, dict)
            or str(existing.get("stage") or "") != str(expected_checkpoint_stage)
        ):
            log.warning(
                "Checkpoint stage compare-and-swap rejected: expected=%s current=%s",
                expected_checkpoint_stage,
                existing.get("stage") if isinstance(existing, dict) else None,
            )
            return False
        if expected_workflow_run_id is not None and (
            not isinstance(existing, dict)
            or str(
                existing.get("workflow_run_id")
                or existing.get("run_id")
                or (
                    f"{int(existing.get('next_v'))}#"
                    f"{int(existing.get('generation_attempt') or 0)}"
                )
            ) != str(expected_workflow_run_id)
        ):
            log.warning("Checkpoint workflow identity compare-and-swap rejected")
            return False

        # Merge with existing — preserve gate_results, master_plan, etc.
        existing_gate_results = {}
        existing_failure_count = 0
        existing_master_plan = master_plan
        existing_reviewer_feedback = reviewer_feedback
        existing_generation_attempt = generation_attempt
        existing_audit_attempt = audit_attempt
        existing_parent2_v = parent2_v
        existing_direction_audit = None
        existing_audit_context = {}
        existing_precommit_attempt = precommit_attempt
        existing_precommit_rework_count = precommit_rework_count
        existing_official_rework_count = official_rework_count
        existing_timeout_extensions = 0
        existing_literature_probe = None
        existing_repo_baseline = None
        existing_prepare_scope_files = []
        existing_runtime_contract_ledger = None
        existing_infra_failure = None
        existing_official_job = None
        existing_repair_baseline_artifact_hash = None
        existing_publication_intent = None
        existing_terminal_gate_outcome = None
        existing_review_attempt_journal = []
        existing_identity_replan_history = []
        existing_epoch_binding = None
        existing_workflow_run_id = ""
        requested_workflow_run_id = str(workflow_run_id or "").strip()
        existing_checkpoint_revision = 0

        if existing and existing.get("next_v") == next_v and existing.get("source_v") == source_v:
            existing_gate_results = existing.get("gate_results", {}) or {}
            existing_failure_count = existing.get("worker_failure_count", 0)
            existing_timeout_extensions = existing.get("timeout_extensions", 0)
            if master_plan is None:
                existing_master_plan = existing.get("master_plan")
            if clear_reviewer_feedback:
                existing_reviewer_feedback = ""
            elif not reviewer_feedback:
                existing_reviewer_feedback = existing.get("reviewer_feedback", "")
            if generation_attempt == 0:
                existing_generation_attempt = existing.get("generation_attempt", 0)
            if audit_attempt is None:
                existing_audit_attempt = existing.get("audit_attempt", 0)
            if precommit_attempt is None:
                existing_precommit_attempt = existing.get("precommit_attempt", 0)
            if precommit_rework_count is None:
                existing_precommit_rework_count = existing.get("precommit_rework_count", 0)
            if official_rework_count is None:
                existing_official_rework_count = existing.get("official_rework_count", 0)
            if timeout_extensions is not None:
                existing_timeout_extensions = int(timeout_extensions)
            if parent2_v is None:
                existing_parent2_v = existing.get("parent2_v")
            existing_direction_audit = existing.get("direction_audit")
            existing_audit_context = existing.get("audit_context", {}) or {}
            existing_literature_probe = existing.get("literature_probe")
            existing_repo_baseline = existing.get("repo_baseline")
            existing_prepare_scope_files = [
                str(item).strip()
                for item in existing.get("prepare_scope_files", []) or []
                if str(item).strip()
            ]
            existing_runtime_contract_ledger = existing.get("runtime_contract_ledger")
            if existing_runtime_contract_ledger is None:
                legacy_master_plan = existing.get("master_plan")
                if isinstance(legacy_master_plan, dict):
                    existing_runtime_contract_ledger = legacy_master_plan.get(
                        "runtime_contract_ledger"
                    )
            existing_infra_failure = existing.get("infra_failure")
            existing_official_job = existing.get("official_job")
            existing_repair_baseline_artifact_hash = existing.get(
                "repair_baseline_artifact_hash"
            )
            existing_publication_intent = existing.get("publication_intent")
            existing_terminal_gate_outcome = existing.get(
                "terminal_gate_outcome"
            )
            existing_review_attempt_journal = deepcopy(
                existing.get("review_attempt_journal") or []
            )
            if identity_replan_history is None:
                existing_identity_replan_history = [
                    str(item)
                    for item in (existing.get("identity_replan_history") or [])
                    if isinstance(item, str)
                ]
            else:
                existing_identity_replan_history = [
                    str(item) for item in identity_replan_history if isinstance(item, str)
                ]
            existing_epoch_binding = existing.get("epoch_binding")
            existing_workflow_run_id = str(
                existing.get("workflow_run_id")
                or expected_workflow_run_id
                or ""
            )
            if (
                requested_workflow_run_id
                and existing_workflow_run_id
                and requested_workflow_run_id != existing_workflow_run_id
            ):
                log.error("Refusing checkpoint workflow identity replacement")
                return False
            existing_checkpoint_revision = int(
                existing.get("checkpoint_revision") or 0
            )
        elif existing:
            active_stage = existing.get("stage")
            dead_stages = {None, "archived", "abandoned"}
            if active_stage not in dead_stages:
                log.warning(
                    "Refusing checkpoint identity mismatch: active v%s/source v%s stage=%s, attempted v%s/source v%s stage=%s",
                    existing.get("next_v"), existing.get("source_v"), active_stage,
                    next_v, source_v, stage,
                )
                try:
                    from system_log import log_system_event
                    log_system_event(
                        "pipeline.identity_mismatch_blocked", "error",
                        f"Blocked checkpoint identity mismatch: active v{existing.get('next_v')} "
                        f"from v{existing.get('source_v')} at {active_stage}; attempted v{next_v} from v{source_v}",
                        {"ckpt_next_v": existing.get("next_v"),
                         "ckpt_source_v": existing.get("source_v"),
                         "ckpt_stage": active_stage,
                         "args_next_v": next_v,
                         "args_source_v": source_v,
                         "args_stage": stage},
                    )
                except Exception:
                    pass
                return False

        # Explicit reset: a newly accepted Master plan starts a fresh durable
        # generation-attempt identity. Critic verdicts are advisory and do not
        # increment this counter or authorize Worker rework.
        if reset_generation_attempt:
            existing_generation_attempt = 0
        if reset_audit_attempt:
            existing_audit_attempt = 0
        if reset_precommit_attempt:
            existing_precommit_attempt = 0
        if timeout_extensions is not None:
            existing_timeout_extensions = int(timeout_extensions)

        old_stage_for_replacement = (
            existing.get("stage") if isinstance(existing, dict) else None
        )
        identity_replan_replacement = bool(
            audit_context_replacement_reason
            == "architecture_policy_identity_replan"
            and stage == "direction_audited"
            and old_stage_for_replacement in {
                "quality_failed",
                "repair_planned",
                "rework_running",
                "direction_audited",
            }
        )
        destructive_identity_reset = bool(
            replace_audit_context or clear_repair_baseline_artifact_hash
        )
        if destructive_identity_reset:
            if not identity_replan_replacement:
                log.error(
                    "Checkpoint destructive field reset is not an authorized "
                    "architecture policy identity replan"
                )
                return False
            replacement = audit_context if isinstance(audit_context, dict) else {}
            replan = replacement.get("architecture_policy_identity_replan")
            prepared = replacement.get("prepared_artifact_contract")
            stale_keys = {
                "strict_policy_identity_refresh",
                "durable_worker_output",
                "durable_worker_failure",
                "worker_execution_failed_replan",
                "quality_native_match_timing_plan",
                "quality_native_match_timing_plan_digest",
                "precommit_eval_plan",
            }
            try:
                explicit_cas = bool(
                    isinstance(expected_checkpoint_revision, int)
                    and not isinstance(expected_checkpoint_revision, bool)
                    and expected_checkpoint_revision > 0
                    and expected_checkpoint_revision
                    == existing_checkpoint_revision
                    and isinstance(expected_checkpoint_stage, str)
                    and bool(expected_checkpoint_stage.strip())
                    and expected_checkpoint_stage == old_stage_for_replacement
                    and isinstance(expected_workflow_run_id, str)
                    and bool(expected_workflow_run_id.strip())
                    and expected_workflow_run_id == existing_workflow_run_id
                )
                replacement_errors = (
                    _identity_replan_replacement_contract_errors(
                        replacement=replacement,
                        next_v=next_v,
                        source_v=source_v,
                        workflow_run_id=existing_workflow_run_id,
                        checkpoint_revision=existing_checkpoint_revision,
                        checkpoint_stage=old_stage_for_replacement,
                        epoch_binding=existing_epoch_binding,
                    )
                )
                if not replacement_errors:
                    replacement_errors.extend(
                        _identity_replan_live_materialization_errors(
                            replacement,
                            candidate_dir=BOTS_DIR / bot_name(next_v),
                            artifact_root=(
                                RESULTS_DIR / "workflow" / "artifacts"
                            ),
                        )
                    )
                replacement_contract_valid = bool(
                    explicit_cas
                    and replace_audit_context
                    and clear_repair_baseline_artifact_hash
                    and master_plan == {}
                    and not replacement_errors
                    and not stale_keys.intersection(replacement)
                    and existing_parent2_v is None
                    and existing_publication_intent is None
                    and existing_official_job is None
                    and existing_infra_failure is None
                )
            except Exception:
                replacement_contract_valid = False
            if not replacement_contract_valid:
                log.error(
                    "Checkpoint architecture identity replan replacement "
                    "contract is invalid: %s",
                    replacement_errors if 'replacement_errors' in locals() else [],
                )
                return False

        if gate_results:
            existing_gate_results.update(gate_results)
        if review_attempt_journal is not None:
            if not isinstance(review_attempt_journal, list):
                log.error("Invalid Reviewer attempt journal projection")
                return False
            # The caller supplies the complete append-only projection.  Never
            # accept truncation or mutation of an already durable prefix.
            if (
                len(review_attempt_journal) < len(existing_review_attempt_journal)
                or review_attempt_journal[: len(existing_review_attempt_journal)]
                != existing_review_attempt_journal
            ):
                log.error("Refusing Reviewer attempt journal rewrite")
                return False
            existing_review_attempt_journal = deepcopy(review_attempt_journal)
        existing_gate_results = _prune_gate_results_for_stage(stage, existing_gate_results)
        if worker_failure_count is not None:
            existing_failure_count = worker_failure_count
        elif worker_invocation_count is not None:
            existing_failure_count = worker_invocation_count
        if direction_audit is not None:
            existing_direction_audit = direction_audit
        if replace_audit_context:
            if not isinstance(audit_context, dict):
                log.error("Audit context replacement requires an object")
                return False
            existing_audit_context = deepcopy(audit_context)
        elif audit_context is not None:
            existing_audit_context.update(audit_context)
        if existing_epoch_binding is None:
            try:
                from checkpoint_schema import build_checkpoint_epoch_binding

                existing_epoch_binding = build_checkpoint_epoch_binding(
                    next_v=next_v,
                    source_v=source_v,
                    parent2_v=existing_parent2_v,
                    audit_context=existing_audit_context,
                    published_high_water=allocation_authority[
                        "published_high_water"
                    ],
                    abandoned_receipt_floor=allocation_authority[
                        "abandoned_receipt_floor"
                    ],
                    abandoned_receipt_head_digest=allocation_authority[
                        "abandoned_receipt_head_digest"
                    ],
                    repo_root=PROJECT_ROOT,
                )
            except Exception as exc:
                errors = list(getattr(exc, "errors", ()) or ())
                log.error(
                    "Refusing checkpoint without a valid strict epoch binding: %s",
                    errors or f"{type(exc).__name__}: {exc}",
                )
                return False
        if literature_probe is not None:
            existing_literature_probe = literature_probe
        if prepare_scope_files is not None:
            existing_prepare_scope_files = sorted({
                *existing_prepare_scope_files,
                *(
                    str(item).strip()
                    for item in prepare_scope_files
                    if str(item).strip()
                ),
            })
        if clear_repair_baseline_artifact_hash:
            if repair_baseline_artifact_hash is not None:
                log.error(
                    "Repair baseline clear cannot carry a replacement hash"
                )
                return False
            existing_repair_baseline_artifact_hash = None
        elif repair_baseline_artifact_hash is not None:
            repair_hash = str(repair_baseline_artifact_hash).strip()
            if not re.fullmatch(r"[0-9a-f]{64}", repair_hash):
                log.error("Invalid repair baseline artifact hash")
                return False
            existing_repair_baseline_artifact_hash = repair_hash

        # Publication is a one-way, immutable transaction.  Persist its intent
        # under the same checkpoint CAS before any Git mutation, then preserve
        # the exact object through every recovery attempt.  A generic checkpoint
        # rewrite must never replace or silently drop it.
        if publication_intent is not None:
            try:
                from publication_transaction import (
                    publication_intent_structure_errors,
                )

                publication_errors = publication_intent_structure_errors(
                    publication_intent
                )
            except Exception as exc:
                log.error(
                    "Publication intent validation failed closed: %s", exc
                )
                return False
            if publication_errors:
                log.error(
                    "Refusing invalid publication intent: %s",
                    publication_errors,
                )
                return False
            if stage != "publishing":
                log.error("Publication intent requires the publishing stage")
                return False
            if existing_publication_intent is not None:
                if existing_publication_intent != publication_intent:
                    log.error("Refusing publication intent replacement")
                    return False
            else:
                old_stage = existing.get("stage") if isinstance(existing, dict) else ""
                if publication_intent.get("origin_checkpoint_stage") != old_stage:
                    log.error("Publication intent origin stage mismatch")
                    return False
                if int(publication_intent.get("origin_checkpoint_revision") or 0) != int(
                    existing_checkpoint_revision
                ):
                    log.error("Publication intent origin revision mismatch")
                    return False
                if str(publication_intent.get("workflow_run_id") or "") != str(
                    existing_workflow_run_id
                ):
                    log.error("Publication intent workflow identity mismatch")
                    return False
                existing_publication_intent = dict(publication_intent)
        if existing_publication_intent is not None and stage != "publishing":
            log.error("A live publication intent cannot leave the publishing stage")
            return False
        if stage == "publishing" and existing_publication_intent is None:
            log.error("Publishing stage requires an immutable publication intent")
            return False

        terminal_stages = {
            "quality_rejected", "review_rejected", "critic_rejected",
        }
        if terminal_gate_outcome is not None:
            if stage not in terminal_stages:
                log.error("Terminal gate outcome requires a terminal gate stage")
                return False
            if existing_terminal_gate_outcome is not None and (
                existing_terminal_gate_outcome != terminal_gate_outcome
            ):
                log.error("Refusing terminal gate outcome replacement")
                return False
            existing_terminal_gate_outcome = deepcopy(terminal_gate_outcome)
        if stage in terminal_stages and existing_terminal_gate_outcome is None:
            log.error("Terminal gate stage requires an immutable outcome")
            return False
        if existing_terminal_gate_outcome is not None and stage not in terminal_stages:
            log.error("A terminal gate outcome cannot leave its terminal stage")
            return False

        if infra_failure is not None or clear_infra_failure:
            from pipeline_infrastructure import infrastructure_failure_digest

            current_infra_digest = infrastructure_failure_digest(existing_infra_failure)
            if expected_infra_failure_digest is None:
                log.error("Infrastructure overlay mutation requires an expected digest")
                return False
            if str(expected_infra_failure_digest) != current_infra_digest:
                log.warning(
                    "Infrastructure overlay compare-and-swap rejected: expected=%s current=%s",
                    expected_infra_failure_digest,
                    current_infra_digest,
                )
                return False
        if clear_infra_failure:
            if not isinstance(existing_infra_failure, dict):
                log.error("Refusing to clear absent infrastructure overlay")
                return False
            if not infra_failure_owner or existing_infra_failure.get("owner_tool") != infra_failure_owner:
                log.error(
                    "Refusing infrastructure clear by %s; owner is %s",
                    infra_failure_owner,
                    existing_infra_failure.get("owner_tool"),
                )
                return False
            existing_infra_failure = None
        elif infra_failure is not None:
            try:
                from pipeline_infrastructure import validate_infrastructure_failure

                infra_errors = validate_infrastructure_failure(infra_failure)
                if infra_errors:
                    log.error("Refusing invalid infrastructure overlay: %s", infra_errors)
                    return False
                existing_infra_failure = dict(infra_failure)
            except Exception as exc:
                log.error("Infrastructure overlay validation failed closed: %s", exc)
                return False
        if isinstance(existing_infra_failure, dict):
            resume_stage = str(existing_infra_failure.get("resume_stage") or "")
            if stage != resume_stage:
                log.error(
                    "Refusing checkpoint stage %s while infrastructure recovery is bound to %s",
                    stage,
                    resume_stage,
                )
                return False

        if official_job is not None or clear_official_job:
            current_official_job_id = (
                str(existing_official_job.get("job_id") or "")
                if isinstance(existing_official_job, dict)
                else ""
            )
            if expected_official_job_id is None:
                log.error("Official job attachment mutation requires an expected job id")
                return False
            if str(expected_official_job_id) != current_official_job_id:
                log.warning(
                    "Official job attachment compare-and-swap rejected: expected=%s current=%s",
                    expected_official_job_id,
                    current_official_job_id,
                )
                return False
        if clear_official_job:
            if not isinstance(existing_official_job, dict):
                log.error("Refusing to clear absent official job attachment")
                return False
            existing_official_job = None
        elif official_job is not None:
            if not isinstance(official_job, dict):
                log.error("Official job attachment must be an object")
                return False
            required_official_job_fields = (
                "schema_version",
                "job_id",
                "identity_digest",
                "candidate_hash",
                "policy_id",
                "state",
                "revision",
            )
            if any(not str(official_job.get(key, "")).strip() for key in required_official_job_fields):
                log.error("Official job attachment is missing required identity fields")
                return False
            existing_official_job = dict(official_job)

        incoming_runtime_contract_ledger = (
            existing_master_plan.get("runtime_contract_ledger")
            if isinstance(existing_master_plan, dict)
            else None
        )
        if reset_runtime_contract_ledger:
            old_stage = existing.get("stage") if isinstance(existing, dict) else None
            reset_allowed, reset_reason = validate_runtime_contract_ledger_reset(
                old_stage,
                stage,
            )
            if not reset_allowed:
                log.error(
                    "Refusing runtime contract ledger reset for %s -> %s: %s",
                    old_stage,
                    stage,
                    reset_reason,
                )
                return False
            if str(runtime_contract_ledger_reset_reason or "") != reset_reason:
                log.error(
                    "Runtime contract ledger reset reason mismatch: requested=%s required=%s",
                    runtime_contract_ledger_reset_reason,
                    reset_reason,
                )
                return False
            if master_plan != {} or incoming_runtime_contract_ledger is not None:
                log.error(
                    "Runtime contract ledger reset requires an explicitly empty master_plan"
                )
                return False
            if expected_runtime_contract_ledger_digest is None:
                log.error(
                    "Runtime contract ledger reset requires an expected ledger digest"
                )
                return False
            try:
                from runtime_architecture_policy import validate_runtime_contract_ledger

                if existing_runtime_contract_ledger is not None:
                    existing_errors = validate_runtime_contract_ledger(
                        existing_runtime_contract_ledger
                    )
                    if existing_errors:
                        log.error(
                            "Refusing reset of invalid runtime contract ledger: %s",
                            existing_errors,
                        )
                        return False
                current_ledger_digest = str(
                    (existing_runtime_contract_ledger or {}).get("ledger_digest") or ""
                )
            except Exception as exc:
                log.error(
                    "Runtime contract ledger reset validation failed closed: %s",
                    exc,
                )
                return False
            if str(expected_runtime_contract_ledger_digest) != current_ledger_digest:
                log.warning(
                    "Runtime contract ledger reset compare-and-swap rejected: "
                    "expected=%s current=%s",
                    expected_runtime_contract_ledger_digest,
                    current_ledger_digest,
                )
                return False
            existing_runtime_contract_ledger = None

        if incoming_runtime_contract_ledger is not None or existing_runtime_contract_ledger is not None:
            try:
                from runtime_architecture_policy import validate_runtime_contract_ledger

                if incoming_runtime_contract_ledger is not None:
                    incoming_errors = validate_runtime_contract_ledger(incoming_runtime_contract_ledger)
                    if incoming_errors:
                        log.error("Refusing invalid runtime contract ledger: %s", incoming_errors)
                        return False
                if existing_runtime_contract_ledger is not None:
                    existing_errors = validate_runtime_contract_ledger(existing_runtime_contract_ledger)
                    if existing_errors:
                        log.error("Existing runtime contract ledger is invalid: %s", existing_errors)
                        return False
                    if incoming_runtime_contract_ledger is None and master_plan is not None:
                        log.error("Refusing master_plan rewrite that drops runtime contract ledger")
                        return False
                    if incoming_runtime_contract_ledger is not None:
                        previous_entries = {
                            str(item.get("contract_digest") or "")
                            for item in existing_runtime_contract_ledger.get("entries") or []
                        }
                        incoming_entries = {
                            str(item.get("contract_digest") or "")
                            for item in incoming_runtime_contract_ledger.get("entries") or []
                        }
                        if not previous_entries.issubset(incoming_entries):
                            log.error(
                                "Refusing runtime contract ledger rewrite/removal: missing=%s",
                                sorted(previous_entries - incoming_entries),
                            )
                            return False
                if incoming_runtime_contract_ledger is not None:
                    existing_runtime_contract_ledger = incoming_runtime_contract_ledger
            except Exception as exc:
                log.error("Runtime contract ledger validation failed closed: %s", exc)
                return False

        # Merge last_stage_change_ts: take max of existing vs current time.
        # This preserves the most recent genuine stage-change time on partial re-writes
        # (e.g. gate_results update without stage change).
        existing_stage_ts = 0.0
        if existing:
            existing_stage_ts = existing.get("last_stage_change_ts", 0.0)
        now_ts = time.time()
        # Validate stage transition and update timestamps
        old_stage = existing.get("stage") if existing else None
        is_valid, reason = validate_stage_transition(old_stage, stage)
        if not is_valid:
            log.warning(
                "Illegal stage transition: %s -> %s (%s). Blocking checkpoint write.",
                old_stage, stage, reason,
            )
            try:
                from system_log import log_system_event
                log_system_event(
                    "pipeline.stage_transition_blocked", "error",
                    f"Blocked illegal stage transition: {old_stage} -> {stage} ({reason})",
                    {"old_stage": old_stage, "new_stage": stage, "reason": reason,
                     "next_v": next_v, "source_v": source_v},
                )
            except Exception:
                pass
            return False
        # touch_stage_timestamp forces last_stage_change_ts to now even when the
        # stage did not change, e.g. the orchestrator's timeout-extension refresh
        # so the watchdog does not immediately re-fire after a cycle resume.
        if touch_stage_timestamp:
            new_stage_ts = now_ts
        else:
            new_stage_ts = now_ts if (old_stage != stage) else existing_stage_ts

        # AUTO-RESET precommit_attempt and timeout_extensions on true rework.
        # Any regression to a code-regeneration stage means this is new bot code,
        # so counters against the previous code snapshot must restart.
        rework_resets_counters = is_rework_reset_transition(old_stage, stage)
        official_job_invalidated = invalidates_official_job_transition(old_stage, stage)
        refresh_repo_baseline = (
            rework_resets_counters
            or _stage_refreshes_repo_baseline(old_stage, stage, existing_gate_results)
        )
        if rework_resets_counters:
            existing_precommit_attempt = 0
            existing_timeout_extensions = 0
        if rework_resets_counters or official_job_invalidated:
            existing_official_job = None
            existing_gate_results.pop("official_full", None)

        # Ensure int type invariants for persisted counters. None arises on a
        # fresh checkpoint when the caller did not pass a counter; defaulting
        # here keeps log correlation complete instead of emitting
        # {"audit": null, ...} for the rest of the generation.
        if existing_generation_attempt is None:
            existing_generation_attempt = 0
        if existing_audit_attempt is None:
            existing_audit_attempt = 0
        if existing_precommit_attempt is None:
            existing_precommit_attempt = 0
        if existing_precommit_rework_count is None:
            existing_precommit_rework_count = 0
        if existing_official_rework_count is None:
            existing_official_rework_count = 0
        run_id = f"{next_v}#{existing_generation_attempt}"
        if not existing_workflow_run_id:
            existing_workflow_run_id = (
                requested_workflow_run_id
                or f"generation:{int(next_v)}:{uuid.uuid4().hex}"
            )
        next_checkpoint_revision = existing_checkpoint_revision + 1
        _contract_checkpoint = {
            "next_v": next_v,
            "source_v": source_v,
            "parent2_v": existing_parent2_v,
            "gate_results": existing_gate_results,
            "stage": stage,
        }
        if refresh_repo_baseline:
            existing_repo_baseline = _capture_repo_baseline(
                stage,
                next_v=next_v,
                source_v=source_v,
                checkpoint=_contract_checkpoint,
            )
        elif not existing_repo_baseline:
            existing_repo_baseline = _capture_repo_baseline(
                stage,
                next_v=next_v,
                source_v=source_v,
                checkpoint=_contract_checkpoint,
            )
        elif isinstance(existing_repo_baseline, dict):
            existing_repo_baseline["evaluation_contract"] = build_evaluation_contract(
                PROJECT_ROOT,
                candidate_v=next_v,
                source_v=source_v,
                checkpoint=_contract_checkpoint,
                stage=stage,
                include_hash=True,
            )

        from checkpoint_schema import (
            CHECKPOINT_SCHEMA_VERSION,
            checkpoint_epoch_errors,
            live_checkpoint_allocation_authority_errors,
            live_checkpoint_parent_authority_errors,
        )

        state = {
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "evaluation_epoch": EVALUATION_EPOCH,
            "epoch_binding": existing_epoch_binding,
            "next_v": next_v, "source_v": source_v, "stage": stage,
            "run_id": run_id,
            "workflow_run_id": existing_workflow_run_id,
            "checkpoint_revision": next_checkpoint_revision,
            "master_plan": existing_master_plan, "reviewer_feedback": existing_reviewer_feedback,
            "generation_attempt": existing_generation_attempt,
            "audit_attempt": existing_audit_attempt,
            "precommit_attempt": existing_precommit_attempt,
            "precommit_rework_count": existing_precommit_rework_count,
            "official_rework_count": existing_official_rework_count,
            "timeout_extensions": existing_timeout_extensions,
            "worker_failure_count": existing_failure_count,
            "gate_results": existing_gate_results,
            "parent2_v": existing_parent2_v,
            "direction_audit": existing_direction_audit,
            "audit_context": existing_audit_context,
            "literature_probe": existing_literature_probe,
            "workflow_profile_id": current_workflow_profile_id,
            "national_execution_mode": current_national_execution_mode,
            "repo_baseline": existing_repo_baseline,
            "prepare_scope_files": existing_prepare_scope_files,
            "runtime_contract_ledger": existing_runtime_contract_ledger,
            "infra_failure": existing_infra_failure,
            "official_job": existing_official_job,
            "repair_baseline_artifact_hash": existing_repair_baseline_artifact_hash,
            "publication_intent": existing_publication_intent,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "last_stage_change_ts": new_stage_ts,
            "last_update_ts": now_ts,  # Always bumps on any checkpoint write
        }
        if existing_review_attempt_journal:
            state["review_attempt_journal"] = existing_review_attempt_journal
        if existing_identity_replan_history:
            state["identity_replan_history"] = existing_identity_replan_history
        if existing_terminal_gate_outcome is not None:
            state["terminal_gate_outcome"] = existing_terminal_gate_outcome

        epoch_errors = checkpoint_epoch_errors(state)
        if stage in terminal_stages:
            try:
                from gate_outcome import validate_terminal_gate_outcome

                terminal_errors = validate_terminal_gate_outcome(
                    state,
                    candidate_dir=get_bot_dir(next_v),
                )
            except Exception as exc:
                terminal_errors = [
                    "terminal_outcome_projection_validation_error:"
                    f"{type(exc).__name__}"
                ]
            if terminal_errors:
                log.error(
                    "Refusing invalid terminal gate outcome: %s",
                    terminal_errors,
                )
                return False
        if not epoch_errors:
            epoch_errors.extend(
                live_checkpoint_parent_authority_errors(
                    state,
                    repo_root=PROJECT_ROOT,
                )
            )
        if not epoch_errors:
            epoch_errors.extend(
                live_checkpoint_allocation_authority_errors(
                    state,
                    published_high_water=allocation_authority[
                        "published_high_water"
                    ],
                    abandoned_receipt_floor=allocation_authority[
                        "abandoned_receipt_floor"
                    ],
                    abandoned_receipt_head_digest=allocation_authority[
                        "abandoned_receipt_head_digest"
                    ],
                    allow_published_target_reconciliation=(
                        _publication_checkpoint_reconciliation_allowed(
                            state,
                            allocation_authority,
                        )
                    ),
                )
            )
        if epoch_errors:
            log.error(
                "Refusing checkpoint whose strict epoch binding does not match "
                "the final CAS projection: %s",
                epoch_errors,
            )
            return False

        # Whole-file atomic publication under the stable sidecar lock.
        _atomic_publish_state_text(
            PIPELINE_STATE_FILE,
            json.dumps(state, indent=2, ensure_ascii=False, allow_nan=False),
        )

        # RC2/RC6: refresh event_bus last-known correlation so events emitted
        # after this stage advance — and especially after clear_pipeline_checkpoint
        # post-commit — still resolve the correct run_id/stage/attempt. This makes
        # stage/attempt correlation automatic for ALL pipeline code, not just call
        # sites that manually bind(). Also invalidates the checkpoint TTL cache so
        # the next emit sees the new stage immediately rather than the stale value.
        try:
            from event_bus import update_last_known, invalidate_ckpt_cache
            update_last_known(
                run_id=run_id,
                stage=stage,
                attempt={"generation": existing_generation_attempt,
                         "audit": existing_audit_attempt,
                         "precommit": existing_precommit_attempt})
            invalidate_ckpt_cache()
        except Exception:
            pass
        return True


def read_pipeline_checkpoint():
    """Return saved pipeline state dict, or None."""
    if not os.path.lexists(PIPELINE_STATE_FILE):
        return None
    try:
        with _locked_state_sidecar(PIPELINE_STATE_FILE, lock_type=fcntl.LOCK_SH):
            raw = _read_regular_state_text(
                PIPELINE_STATE_FILE,
                allow_missing=False,
            )
        checkpoint = json.loads(raw)
        from pipeline_infrastructure import normalize_checkpoint_infrastructure

        return normalize_checkpoint_infrastructure(checkpoint)
    except Exception:
        return None


def clear_pipeline_checkpoint(
    *,
    expected_workflow_run_id=None,
    expected_next_v=None,
    expected_source_v=None,
    expected_checkpoint_revision=None,
    expected_checkpoint_stage=None,
):
    """Delete pipeline checkpoint (called on successful commit).

    Uses exclusive lock to prevent race with concurrent writes.
    """
    previous = None
    try:
        _preflight_state_sidecar(PIPELINE_STATE_FILE)
    except OSError:
        return False
    guard = _locked_state_sidecar(
        PIPELINE_STATE_FILE,
        lock_type=fcntl.LOCK_EX,
    )
    with guard:
        try:
            raw = _read_regular_state_text(
                PIPELINE_STATE_FILE,
                allow_missing=True,
            )
        except (OSError, UnicodeError):
            return False
        if raw.strip():
            try:
                previous = json.loads(raw)
            except Exception:
                previous = None
        if (
            expected_workflow_run_id is not None
            or expected_next_v is not None
            or expected_source_v is not None
            or expected_checkpoint_revision is not None
            or expected_checkpoint_stage is not None
        ):
            if not isinstance(previous, dict):
                return False
            actual_workflow_run_id = str(
                previous.get("workflow_run_id")
                or previous.get("run_id")
                or (
                    f"{int(previous.get('next_v'))}#"
                    f"{int(previous.get('generation_attempt') or 0)}"
                )
            )
            if (
                expected_workflow_run_id is not None
                and actual_workflow_run_id != str(expected_workflow_run_id)
            ):
                return False
            if (
                expected_next_v is not None
                and previous.get("next_v") != expected_next_v
            ):
                return False
            if (
                expected_source_v is not None
                and previous.get("source_v") != expected_source_v
            ):
                return False
            if (
                expected_checkpoint_revision is not None
                and int(previous.get("checkpoint_revision") or 0)
                != int(expected_checkpoint_revision)
            ):
                return False
            if (
                expected_checkpoint_stage is not None
                and str(previous.get("stage") or "")
                != str(expected_checkpoint_stage)
            ):
                return False
        # Unlink under the stable sidecar lock so writers cannot race a retired
        # checkpoint inode.
        PIPELINE_STATE_FILE.unlink(missing_ok=True)
        _fsync_directory(PIPELINE_STATE_FILE.parent)
    try:
        from event_bus import emit
        next_v = previous.get("next_v") if previous else None
        gen_attempt = previous.get("generation_attempt", 0) if previous else 0
        audit_attempt = previous.get("audit_attempt", 0) if previous else 0
        precommit_attempt = previous.get("precommit_attempt", 0) if previous else 0
        emit(
            "pipeline.checkpoint_cleared", "info",
            "Pipeline checkpoint cleared",
            run_id=(previous.get("run_id") if previous else None) or (
                f"{next_v}#{gen_attempt}" if next_v is not None else None
            ),
            stage=previous.get("stage") if previous else None,
            attempt={"generation": gen_attempt, "audit": audit_attempt,
                     "precommit": precommit_attempt},
            next_v=next_v,
            source_v=previous.get("source_v") if previous else None,
        )
    except Exception:
        try:
            from system_log import _write_system_event_raw
            _write_system_event_raw(
                "pipeline.checkpoint_cleared", "info",
                "Pipeline checkpoint cleared",
                {"next_v": previous.get("next_v") if previous else None,
                 "source_v": previous.get("source_v") if previous else None,
                 "stage": previous.get("stage") if previous else None,
                 "run_id": previous.get("run_id") if previous else None,
                 "category": "pipeline.checkpoint_cleared"},
            )
        except Exception:
            pass
    return True


# ──────────────────────────────────────────────
# UI Interface
# ──────────────────────────────────────────────

class BaseUI:
    def log_history(self, msg, status="info"): pass
    def set_status(self, msg, is_working=False): pass
    def log_io(self, msg, stream_type="default", role=""): pass
    def clear_io(self): pass
    def update_eval_table(self, ratings, active_bots): pass
    def update_daemon_status(self, stats, ratings): pass
    def set_header(self, msg): pass
    def update_cost(self, role, cost_usd, usage): pass
    def begin_generation_cost(self, generation_id, spent_usd, policy_receipt=None): pass
    def reset_gen_cost(self): pass
    def update_metrics(self, metrics): pass
    def emit_tool_call(self, tool_name: str, args: dict, role: str = ""): pass


class NullUI(BaseUI):
    """Explicit no-op UI for headless infrastructure entry points."""


_NULL_UI = NullUI()


def resolve_ui(ui=None):
    """Return a concrete UI object while preserving an injected UI unchanged."""
    return _NULL_UI if ui is None else ui


# ──────────────────────────────────────────────
# Bot Directory & Status
# ──────────────────────────────────────────────

def count_lines(path):
    try:
        with open(path, "r", errors="ignore") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def pair_key(a, b):
    return f"{a} vs {b}" if a < b else f"{b} vs {a}"


def get_bot_dir(version):
    """Return only the canonical active-namespace directory.

    Archived/reaped trees are never a transparent source, candidate or
    opponent fallback.  Audit callers must address an archive explicitly.
    """

    return BOTS_DIR / bot_name(version)


def get_logs_dir(version):
    d = RESULTS_DIR / f"v{version}" / "logs"
    os.makedirs(d, exist_ok=True)
    return d


def _tagged_bot_versions():
    """Return strict-policy versions backed by completion tags.

    Completion tags through the archived high-water remain immutable audit
    evidence, but they are not members of ``national_tcp_policy_v1``.
    """
    tag_versions = set()
    for tag in _git("tag", "-l", bot_tag_glob(), check=False).strip().splitlines():
        version = parse_tag_version(tag)
        if version is not None and version >= FIRST_STRICT_POLICY_VERSION:
            tag_versions.add(version)
    return tag_versions


def _bot_version_from_name(bot_name):
    return parse_bot_version(bot_name)


def load_reaped_bot_versions():
    """Return durable reaped versions, raising when lifecycle state is unavailable."""
    from national_epoch_registry import load_registry_state

    state = load_registry_state(
        PROJECT_ROOT,
        legacy_ledger=REAPED_BOTS_FILE,
        include_history=False,
    )
    return set(state.require_reaped_versions())


def record_reaped_bot(bot_name, *, reason="", data=None):
    """Durably tombstone a bot before recording the advisory runtime ledger."""
    version = _bot_version_from_name(bot_name)
    if version is None:
        raise ValueError(f"invalid national bot label: {bot_name}")
    push_enabled = evolution_git_push_enabled()
    push_required = evolution_git_push_required()
    if push_required and not push_enabled:
        raise RuntimeError(
            "durable reaping requires EVOLUTION_GIT_PUSH=1 in the evolution runtime"
        )
    from national_epoch_registry import create_reaped_tombstone

    mutation = create_reaped_tombstone(
        version,
        repo_root=PROJECT_ROOT,
        legacy_ledger=REAPED_BOTS_FILE,
    )
    tombstone_tag = f"national-reaped-v{version}"
    pushed = False
    if push_enabled or push_required:
        pushed = git_push_refs(tombstone_tag)
        if push_required and not pushed:
            raise RuntimeError(f"failed to publish durable reaped tombstone {tombstone_tag}")
    try:
        from official_eligibility import clear_registry_state_cache

        clear_registry_state_cache()
    except Exception:
        pass
    entry = {
        "ts": time.time(),
        "bot": bot_name,
        "version": version,
        "reason": reason,
        "data": data or {},
        "registry": {
            "tombstone_tag": tombstone_tag,
            "created_tags": list(mutation.created_tags),
            "pushed": pushed,
        },
    }
    os.makedirs(RESULTS_DIR, exist_ok=True)
    append_locked_jsonl(REAPED_BOTS_FILE, entry)
    return entry


_REMOTE_PUBLICATION_CACHE_LOCK = threading.RLock()
_REMOTE_PUBLICATION_CACHE_CONDITION = threading.Condition(
    _REMOTE_PUBLICATION_CACHE_LOCK
)
_REMOTE_PUBLICATION_CACHE = {
    "key": None,
    "checked_at": 0.0,
    "versions": frozenset(),
    "generation": 0,
    "inflight_key": None,
    "inflight_generation": None,
}


def _clear_remote_publication_cache():
    with _REMOTE_PUBLICATION_CACHE_CONDITION:
        _REMOTE_PUBLICATION_CACHE.update({
            "key": None,
            "checked_at": 0.0,
            "versions": frozenset(),
            "generation": int(
                _REMOTE_PUBLICATION_CACHE.get("generation") or 0
            ) + 1,
        })
        _REMOTE_PUBLICATION_CACHE_CONDITION.notify_all()


def _remote_published_completion_versions(tag_versions) -> set[int]:
    """Return versions whose exact completion/high-water refs are on origin.

    In the long-running evolution checkout a local annotated tag is only a
    recoverable intermediate state.  It must not restore ``.completed`` or
    enter the active pool until origin independently exposes both annotated
    refs and its main branch contains the peeled publication commit.
    """

    versions = tuple(sorted({int(item) for item in tag_versions}))
    if not versions:
        return set()
    local_rows = []
    for version in versions:
        completion = bot_tag(version)
        high_water = f"national-high-water-v{version}"
        local_rows.append((
            version,
            _git("rev-parse", f"refs/tags/{completion}", check=False).strip(),
            _git(
                "rev-parse",
                f"refs/tags/{completion}^{{commit}}",
                check=False,
            ).strip(),
            _git("rev-parse", f"refs/tags/{high_water}", check=False).strip(),
            _git(
                "rev-parse",
                f"refs/tags/{high_water}^{{commit}}",
                check=False,
            ).strip(),
        ))
    cache_key = tuple(local_rows)
    # A Dashboard can ask for status, health, evolution state and strength at
    # the same time.  Once the five-second proof cache expires those callers
    # must share one remote transaction; otherwise each observer launches its
    # own ``git ls-remote``/fetch and a slow origin amplifies into an ASGI
    # outage.  Mutation/launch callers still wait for this exact fresh proof --
    # no stale remote result is accepted at an effect boundary.
    while True:
        now = time.monotonic()
        with _REMOTE_PUBLICATION_CACHE_CONDITION:
            if (
                _REMOTE_PUBLICATION_CACHE.get("key") == cache_key
                and now
                - float(_REMOTE_PUBLICATION_CACHE.get("checked_at") or 0.0)
                <= 5.0
            ):
                return set(_REMOTE_PUBLICATION_CACHE.get("versions") or ())
            inflight_key = _REMOTE_PUBLICATION_CACHE.get("inflight_key")
            if inflight_key is not None:
                _REMOTE_PUBLICATION_CACHE_CONDITION.wait()
                continue
            refresh_generation = int(
                _REMOTE_PUBLICATION_CACHE.get("generation") or 0
            )
            _REMOTE_PUBLICATION_CACHE["inflight_key"] = cache_key
            _REMOTE_PUBLICATION_CACHE["inflight_generation"] = (
                refresh_generation
            )
            break
    try:
        raw = _git(
            "ls-remote",
            "origin",
            "refs/heads/main",
            f"refs/tags/{ACTIVE_TAG_PREFIX}*",
            "refs/tags/national-high-water-v*",
        )
        remote: dict[str, str] = {}
        for line in raw.splitlines():
            oid, separator, ref = line.partition("\t")
            if separator and oid and ref:
                remote[ref] = oid
        remote_main = remote.get("refs/heads/main", "")
        if len(remote_main) != 40:
            raise RuntimeError("remote main ref is missing")
        current_remote_tracking = _git(
            "rev-parse", "refs/remotes/origin/main", check=False
        ).strip()
        if current_remote_tracking != remote_main:
            _git(
                "fetch",
                "--no-tags",
                "origin",
                "refs/heads/main:refs/remotes/origin/main",
            )
        verified: set[int] = set()
        for version, tag_object, commit_oid, water_object, water_commit in local_rows:
            completion = bot_tag(version)
            high_water = f"national-high-water-v{version}"
            if not all((tag_object, commit_oid, water_object, water_commit)):
                continue
            if _git(
                "cat-file", "-t", f"refs/tags/{completion}", check=False
            ).strip() != "tag":
                continue
            if _git(
                "cat-file", "-t", f"refs/tags/{high_water}", check=False
            ).strip() != "tag":
                continue
            if (
                remote.get(f"refs/tags/{completion}") != tag_object
                or remote.get(f"refs/tags/{completion}^{{}}") != commit_oid
                or remote.get(f"refs/tags/{high_water}") != water_object
                or remote.get(f"refs/tags/{high_water}^{{}}") != water_commit
                or water_commit != commit_oid
                or not _git_command_succeeds(
                    "merge-base", "--is-ancestor", commit_oid, remote_main
                )
            ):
                continue
            verified.add(version)
    except Exception as exc:
        log.error(
            "Remote publication proof unavailable; active pool fails closed: %s",
            exc,
        )
        verified = set()
    with _REMOTE_PUBLICATION_CACHE_CONDITION:
        refresh_is_current = bool(
            _REMOTE_PUBLICATION_CACHE.get("generation")
            == refresh_generation
            and _REMOTE_PUBLICATION_CACHE.get("inflight_key") == cache_key
            and _REMOTE_PUBLICATION_CACHE.get("inflight_generation")
            == refresh_generation
        )
        if refresh_is_current:
            _REMOTE_PUBLICATION_CACHE.update({
                "key": cache_key,
                "checked_at": time.monotonic(),
                "versions": frozenset(verified),
            })
        _REMOTE_PUBLICATION_CACHE["inflight_key"] = None
        _REMOTE_PUBLICATION_CACHE["inflight_generation"] = None
        _REMOTE_PUBLICATION_CACHE_CONDITION.notify_all()
    # Cache invalidation is an authority movement.  A remote response which
    # began before that movement is not allowed to escape to its caller.
    return verified if refresh_is_current else set()


def _ensure_completed_sentinels_for_tagged_bots(tag_versions=None, reaped_versions=None):
    """Restore local .completed sentinels for bot dirs that already have tags.

    The sentinel is runtime metadata and may be absent in isolated clones because
    it is gitignored. The active-epoch tag remains the authoritative completion proof,
    so restoring the local sentinel keeps runtime active-bot discovery consistent
    without trusting untagged or abandoned directories. Intentionally reaped bots
    are skipped because they are tagged but no longer active.
    """
    if tag_versions is None:
        tag_versions = _tagged_bot_versions()
    if reaped_versions is None:
        try:
            reaped_versions = load_reaped_bot_versions()
        except Exception as exc:
            log.error("National reaped registry unavailable; refusing sentinel restore: %s", exc)
            return []
    if not tag_versions or not BOTS_DIR.exists():
        return []

    restored = []
    for version in sorted(tag_versions):
        if version in reaped_versions:
            continue
        bot_dir = BOTS_DIR / bot_name(version)
        sentinel = bot_dir / ".completed"
        if not bot_dir.is_dir() or sentinel.exists():
            continue
        if not is_active_bot_protocol_eligible(version):
            continue
        try:
            sentinel.write_text(f"restored from {bot_tag(version)} tag\n", encoding="utf-8")
            restored.append(version)
        except OSError as exc:
            log.warning("Failed to restore .completed sentinel for %s: %s", bot_name(version), exc)

    if restored:
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.completed_sentinel_restored",
                "warning",
                f"Restored .completed sentinels for tagged bots: {restored}",
                {"versions": restored},
            )
        except Exception:
            pass
    return restored


def _incomplete_checkpoint_publication_versions(tag_versions) -> set[int]:
    """Keep a locally tagged in-flight publication out of the active pool.

    A completion tag can exist before the publication transaction has proven
    its remote refs (when required), materialized the durable sentinel, and
    cleared the checkpoint.  In particular, local-only deployments must not
    let the generic tag-to-sentinel repair path skip those final transaction
    phases after a crash.  Once the exact intent-bound sentinel exists, the
    candidate may be observed as complete while the final checkpoint CAS is
    retried.
    """

    versions = {int(item) for item in (tag_versions or set())}
    if not versions:
        return set()
    try:
        checkpoint = read_pipeline_checkpoint()
    except Exception:
        return set()
    if not isinstance(checkpoint, dict) or checkpoint.get("stage") != "publishing":
        return set()
    try:
        version = int(checkpoint.get("next_v"))
    except (TypeError, ValueError):
        return set()
    if version not in versions:
        return set()

    intent = checkpoint.get("publication_intent")
    try:
        from publication_transaction import publication_intent_checkpoint_errors

        intent_errors = publication_intent_checkpoint_errors(intent, checkpoint)
    except Exception:
        intent_errors = ["publication_intent_validation_unavailable"]
    publication_id = (
        str(intent.get("publication_id") or "")
        if isinstance(intent, dict)
        else ""
    )
    sentinel = BOTS_DIR / bot_name(version) / ".completed"
    try:
        sentinel_matches = (
            bool(publication_id)
            and sentinel.is_file()
            and not sentinel.is_symlink()
            and sentinel.read_text(encoding="utf-8")
            == f"publication_id={publication_id}\n"
        )
    except OSError:
        sentinel_matches = False
    return {version} if intent_errors or not sentinel_matches else set()


def active_native_contract_filter_enabled() -> bool:
    # The policy epoch has no compatibility escape hatch.  An environment flag
    # cannot reintroduce archived Botzone/strategy artifacts into active roles.
    if EVALUATION_EPOCH == "national_tcp_policy_v1":
        return True
    raw = os.environ.get("POK_ACTIVE_NATIVE_CONTRACT_FILTER")
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


_ACTIVE_BOT_PROTOCOL_CACHE: dict[tuple, tuple[str, ...]] = {}


def _bot_protocol_fingerprint(bot_dir: Path) -> tuple[tuple, ...]:
    files: list[tuple] = []
    if not bot_dir.exists():
        return (("<missing>", 0, 0),)
    for path in sorted(bot_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            st = path.stat()
            rel = str(path.relative_to(bot_dir)).replace(os.sep, "/")
            files.append(
                (
                    rel,
                    int(st.st_mtime_ns),
                    int(st.st_ctime_ns),
                    int(st.st_size),
                    int(st.st_ino),
                )
            )
        except OSError:
            continue
    return tuple(files)


def active_bot_protocol_errors(
    version: int,
    *,
    quarantine_health: dict | None = None,
) -> list[str]:
    """Return active-pool protocol errors for a tagged bot version.

    The strict namespace resolver is the first authority.  It never searches
    the archive and requires the raw-TCP runtime manifest, typed policy ABI and
    epoch receipt before the implementation-level native checks run.
    """

    if not active_native_contract_filter_enabled():
        return []
    bot_dir = BOTS_DIR / bot_name(version)
    fingerprint = _bot_protocol_fingerprint(bot_dir)
    cache_key = (
        int(version),
        str(bot_dir.resolve()),
        fingerprint,
        EVALUATION_EPOCH,
    )
    cached = _ACTIVE_BOT_PROTOCOL_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)

    spec = resolve_national_bot_spec(
        bot_dir,
        ROLE_CANDIDATE,
        repo_root=BOTS_DIR.parent,
        require_completion=False,
        require_certificate=False,
    )
    errors = list(spec.issues)
    if not errors:
        try:
            from national_native import check_native_contract
            errors.extend(
                check_native_contract(
                    bot_dir,
                    require_current_stream_decoder=True,
                    require_current_decision_runtime=True,
                )
            )
        except Exception as exc:
            errors.append(f"native_contract_check_error: {type(exc).__name__}: {str(exc)[:200]}")
    stale_keys = [
        key for key in _ACTIVE_BOT_PROTOCOL_CACHE
        if key[0] == int(version) and key[1] == str(bot_dir.resolve())
    ]
    for key in stale_keys:
        _ACTIVE_BOT_PROTOCOL_CACHE.pop(key, None)
    _ACTIVE_BOT_PROTOCOL_CACHE[cache_key] = tuple(errors)
    return errors


def is_active_bot_protocol_eligible(
    version: int,
    *,
    quarantine_health: dict | None = None,
) -> bool:
    return not active_bot_protocol_errors(
        version,
        quarantine_health=quarantine_health,
    )


_ORIGINAL_IS_ACTIVE_BOT_PROTOCOL_ELIGIBLE = is_active_bot_protocol_eligible


def _protocol_eligible_for_discovery(version: int, quarantine_health: dict | None) -> bool:
    """Reuse one verified policy report while preserving test/plugin overrides."""

    if is_active_bot_protocol_eligible is _ORIGINAL_IS_ACTIVE_BOT_PROTOCOL_ELIGIBLE:
        return is_active_bot_protocol_eligible(
            version,
            quarantine_health=quarantine_health,
        )
    return bool(is_active_bot_protocol_eligible(version))


def _target_rel(path, version):
    raw = str(path).strip()
    if not raw:
        return ""
    raw = raw.replace("\\", "/")
    raw = _TARGET_ANNOTATION_RE.sub("", raw).strip()
    # 循环剥离任意层 bots/{active_bot}{N}/ 前缀（含 source_v + 双重嵌套）。
    # root-cause-audit 2026-06-21: Master context (agent_master.py:100) 注入
    # bots/{bot}{source_v}/ 路径，worker 非确定性地把它写进 target_files，甚至双重嵌套。
    # 循环剥离直到无版本前缀。
    while True:
        stripped = strip_bot_path_prefix(raw)
        if stripped == raw:
            break
        raw = stripped
    return raw


def _discover_active_bots(
    *,
    repair_completed_sentinels: bool,
    require_completed_sentinel: bool = True,
    ledger_fresh: bool = True,
) -> list[str]:
    """Active bots = tagged, completed, and protocol-eligible bots.

    Trust model mirrors find_current_v(): the git tag for the active epoch is the single
    authoritative completion proof. A bare .completed file (written by prepare
    or left behind by a crashed/never-committed generation) is NOT trusted —
    it is exactly how a "ghost bot" like v107 (completed-but-untagged) leaked
    into find_latest_active_v() and was used as an evolution source.

    In the national TCP policy epoch, the typed manifest/receipt ABI and a full
    signed official certificate are also mandatory.  Archived bot directories
    are never traversed.

    Collecting all tags once here (instead of calling git_has_tag per bot)
    keeps this O(1 git call) regardless of bot count, plus local file checks for
    protocol eligibility.
    """
    tag_versions = _tagged_bot_versions()
    if evolution_git_push_required():
        # Never allow a local-only recovery tag to manufacture lifecycle
        # completion while required origin publication is still pending.
        tag_versions = set(tag_versions).intersection(
            _remote_published_completion_versions(tag_versions)
        )
    tag_versions = set(tag_versions).difference(
        _incomplete_checkpoint_publication_versions(tag_versions)
    )
    try:
        reaped_versions = load_reaped_bot_versions()
    except Exception as exc:
        log.error("National reaped registry unavailable; active pool fails closed: %s", exc)
        try:
            from system_log import log_system_event

            log_system_event(
                "pipeline.national_epoch_registry_unavailable",
                "error",
                "National epoch lifecycle registry unavailable; active pool disabled",
                {"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
            )
        except Exception:
            pass
        return []
    if repair_completed_sentinels:
        _ensure_completed_sentinels_for_tagged_bots(tag_versions, reaped_versions)

    bots = []
    if BOTS_DIR.exists():
        for d in os.listdir(BOTS_DIR):
            v = parse_bot_version(d)
            if v is None or not d.startswith(ACTIVE_BOT_PREFIX):
                continue
            completed = (BOTS_DIR / d / ".completed").exists()
            if os.path.isdir(BOTS_DIR / d) and (
                completed or not require_completed_sentinel
            ):
                if (
                    v in tag_versions
                    and v not in reaped_versions
                    and _protocol_eligible_for_discovery(v, None)
                    and (
                        _official_parent_eligible(
                            BOTS_DIR / d,
                            ledger_fresh=False,
                        )
                        if (
                            not ledger_fresh
                            and _official_parent_eligible
                            is _ORIGINAL_OFFICIAL_PARENT_ELIGIBLE
                        )
                        else _official_parent_eligible(BOTS_DIR / d)
                    )
                ):
                    bots.append(d)
    return sorted(bots, key=version_sort_key)


def get_active_bots():
    """Return active bots and repair missing sentinels for trusted tagged bots."""

    return _discover_active_bots(repair_completed_sentinels=True)


def get_active_bots_read_only(*, ledger_fresh: bool = True):
    """Return active bots without performing any filesystem repair.

    Read-only HTTP/catalog code must use this API so a GET request cannot create
    completion sentinels or otherwise mutate the evolution checkout.
    """

    return _discover_active_bots(
        repair_completed_sentinels=False,
        ledger_fresh=ledger_fresh,
    )


def get_published_active_bots_read_only(*, ledger_fresh: bool = True):
    """Return tagged active artifacts without requiring a local sentinel.

    View-only clones do not carry the gitignored ``.completed`` cache. Git tag,
    artifact, protocol, lifecycle, and official eligibility checks remain
    mandatory, so omitting that cache does not weaken completion authority.
    """

    return _discover_active_bots(
        repair_completed_sentinels=False,
        require_completed_sentinel=False,
        ledger_fresh=ledger_fresh,
    )


def _official_parent_eligible(
    bot_dir: Path,
    *,
    ledger_fresh: bool = True,
) -> bool:
    try:
        spec = resolve_national_bot_spec(
            bot_dir,
            ROLE_PARENT_SOURCE,
            repo_root=BOTS_DIR.parent,
            ledger_fresh=ledger_fresh,
        )
        if not spec.eligible:
            log.warning(
                "Strict parent eligibility rejected %s: %s",
                bot_dir.name,
                list(spec.issues),
            )
        return spec.eligible
    except Exception as exc:
        log.error(
            "Official active-pool eligibility failed closed for %s: %s",
            bot_dir.name,
            exc,
        )
        return False


_ORIGINAL_OFFICIAL_PARENT_ELIGIBLE = _official_parent_eligible


def version_namespace_authority():
    """Return the canonical paired/unpaired annotated publication-ref snapshot."""

    return resolve_version_namespace_authority(
        lambda *args: _git(*args, check=False)
    )


def find_current_v():
    """Return the immutable version-authority high-water.

    Only annotated completion/high-water tags which peel to commits advance the
    published namespace.  Directory names, sentinels, bare commits, checkpoint
    counters and runtime ledgers are deliberately absent from this read.
    """

    authority = version_namespace_authority()
    return int(authority.high_water)


def find_latest_active_v():
    """Find the highest version in the strict published active pool.
    Returns 0 if no active bots exist.
    """
    active = get_active_bots()
    if not active:
        return 0
    return max(version_sort_key(b) for b in active)


# ──────────────────────────────────────────────
# Ratings
# ──────────────────────────────────────────────

def load_ratings():
    """Load Glicko-2 ratings with shared lock."""
    from evaluation_data_identity import ensure_evaluation_data_identity

    ensure_evaluation_data_identity(RESULTS_DIR)
    data = read_locked_json(RATINGS_FILE)
    if not data:
        return {}
    try:
        return {name: Glicko2Player.from_dict(d) for name, d in data.items()}
    except Exception:
        return {}


def load_daemon_stats():
    """Load daemon stats."""
    return read_locked_json(STATS_FILE, default={"pairs": {}, "total_games": 0})


def _is_shutdown(event) -> bool:
    """Check if a shutdown signal is set. Accepts asyncio.Event, ShutdownManager, or None."""
    if event is None:
        return False
    if hasattr(event, 'is_set'):
        return event.is_set()
    if hasattr(event, 'is_shutting_down'):
        return event.is_shutting_down
    return False


def _log_eval_wait_event(event_type: str, severity: str, message: str, **data) -> None:
    try:
        from system_log import log_system_event
        log_system_event(event_type, severity, message, data)
    except Exception:
        pass


async def wait_for_daemon_eval(
    bot_name,
    timeout=DAEMON_EVAL_TIMEOUT,
    min_games=MIN_GAMES_FOR_EVAL,
    ui=None,
    shutdown_event=None,
    rd_threshold=90,
    rd_min_games=30,
):
    """Wait for daemon to evaluate a new bot (async, non-blocking).

    Returns True when either:
      - games >= min_games (hard threshold), OR
      - rd < EVAL_RD_THRESHOLD and games >= EVAL_RD_MIN_GAMES (confidence-based early exit)
    Returns False on timeout or shutdown signal.
    """
    from daemon_management import daemon_proc, _daemon_lock

    # RD-based confidence early-exit. Now that decay_rd uses the official Glicko-2
    # formula (no 150 floor), RD genuinely converges as a bot accumulates games.
    # Defaults keep the historical 90/30 gate; workflow profiles may relax or
    # tighten those values for rating backends with different sample semantics.
    EVAL_RD_THRESHOLD = float(rd_threshold)
    EVAL_RD_MIN_GAMES = int(rd_min_games)

    start = time.time()
    cached_bot_stats = None
    bot_stats_mtime = 0
    ratings_mtime = 0
    cached_rd = None
    last_log = start
    last_daemon_dead_log = 0.0

    _log_eval_wait_event(
        "pipeline.eval_wait_start",
        "info",
        f"Waiting for {bot_name} evaluation ({min_games} games or low RD)",
        bot=bot_name,
        min_games=min_games,
        timeout_sec=timeout,
        rd_threshold=EVAL_RD_THRESHOLD,
        rd_min_games=EVAL_RD_MIN_GAMES,
    )

    while time.time() - start < timeout:
        if _is_shutdown(shutdown_event):
            games = (cached_bot_stats or {}).get(bot_name, {}).get("games", 0)
            _log_eval_wait_event(
                "pipeline.eval_wait_shutdown",
                "warn",
                f"Evaluation wait interrupted for {bot_name} during shutdown",
                bot=bot_name,
                games=games,
                min_games=min_games,
                elapsed_sec=round(time.time() - start, 2),
            )
            return False

        if BOT_STATS_FILE.exists():
            mt = os.path.getmtime(BOT_STATS_FILE)
            if mt != bot_stats_mtime:
                bot_stats_mtime = mt
                cached_bot_stats = read_locked_json(BOT_STATS_FILE, default={})
        if cached_bot_stats is None:
            cached_bot_stats = {}

        games = cached_bot_stats.get(bot_name, {}).get("games", 0)
        if games >= min_games:
            _log_eval_wait_event(
                "pipeline.eval_wait_ready",
                "success",
                f"{bot_name} evaluation ready: {games}/{min_games} games",
                bot=bot_name,
                games=games,
                min_games=min_games,
                elapsed_sec=round(time.time() - start, 2),
                reason="min_games",
            )
            return True

        # RD-based early exit
        if games >= EVAL_RD_MIN_GAMES and RATINGS_FILE.exists():
            mt = os.path.getmtime(RATINGS_FILE)
            if mt != ratings_mtime:
                ratings_mtime = mt
                try:
                    ratings = load_ratings()
                    player = ratings.get(bot_name)
                    cached_rd = player.rd if player else None
                except Exception:
                    cached_rd = None
            if cached_rd is not None and cached_rd < EVAL_RD_THRESHOLD:
                if ui:
                    ui.log_history(f"{bot_name} 评估就绪: rd={cached_rd:.1f} (<{EVAL_RD_THRESHOLD}), {games} 场", "success")
                _log_eval_wait_event(
                    "pipeline.eval_wait_ready",
                    "success",
                    f"{bot_name} evaluation ready: rd={cached_rd:.1f}, games={games}",
                    bot=bot_name,
                    games=games,
                    min_games=min_games,
                    rd=round(cached_rd, 2),
                    rd_threshold=EVAL_RD_THRESHOLD,
                    elapsed_sec=round(time.time() - start, 2),
                    reason="rd_threshold",
                )
                return True

        if time.time() - last_log >= EVAL_WAIT_PROGRESS_INTERVAL_SEC:
            elapsed = int(time.time() - start)
            rd_info = f", rd={cached_rd:.1f}" if cached_rd else ""
            if ui:
                ui.log_history(f"等待 {bot_name} 评估: {games}/{min_games} 场 ({elapsed}s{rd_info})", "info")
            _log_eval_wait_event(
                "pipeline.eval_wait_progress",
                "info",
                f"Waiting for {bot_name} evaluation: {games}/{min_games} games ({elapsed}s{rd_info})",
                bot=bot_name,
                games=games,
                min_games=min_games,
                rd=round(cached_rd, 2) if cached_rd is not None else None,
                elapsed_sec=elapsed,
                progress_interval_sec=EVAL_WAIT_PROGRESS_INTERVAL_SEC,
            )
            last_log = time.time()

        # Check daemon health every iteration — daemon may crash after producing
        # partial results, leaving us waiting the full timeout.
        # (Not gated by the 30s log interval — crashes need fast detection.)
        with _daemon_lock:
            proc = daemon_proc
        if proc is not None and proc.poll() is not None:
            if games >= min_games:
                if ui:
                    ui.log_history(f"Daemon 已终止 (rc={proc.returncode})，但已有 {games} 场 (≥{min_games})，继续", "warn")
                _log_eval_wait_event(
                    "pipeline.eval_wait_ready",
                    "success",
                    f"Daemon exited but {bot_name} already has {games}/{min_games} games",
                    bot=bot_name,
                    games=games,
                    min_games=min_games,
                    daemon_returncode=proc.returncode,
                    elapsed_sec=round(time.time() - start, 2),
                    reason="min_games_after_daemon_exit",
                )
                return True
            else:
                if ui:
                    ui.log_history(f"Daemon 已终止 (rc={proc.returncode})，仅 {games}/{min_games} 场，等待重启...", "error")
                if time.time() - last_daemon_dead_log >= EVAL_WAIT_PROGRESS_INTERVAL_SEC:
                    _log_eval_wait_event(
                        "pipeline.eval_wait_daemon_dead",
                        "warn",
                        f"Daemon exited while waiting for {bot_name}: {games}/{min_games} games",
                        bot=bot_name,
                        games=games,
                        min_games=min_games,
                        daemon_returncode=proc.returncode,
                        elapsed_sec=round(time.time() - start, 2),
                    )
                    last_daemon_dead_log = time.time()
                # Don't return False — daemon_monitor_thread may restart it.
                # Continue waiting until timeout expires.


        await asyncio.sleep(5)
    if ui:
        games = cached_bot_stats.get(bot_name, {}).get("games", 0)
        ui.log_history(f"评估超时 {bot_name}: 仅 {games}/{min_games} 场 ({int(time.time()-start)}s)", "warn")
    games = (cached_bot_stats or {}).get(bot_name, {}).get("games", 0)
    _log_eval_wait_event(
        "pipeline.eval_wait_timeout",
        "warn",
        f"Evaluation wait timed out for {bot_name}: {games}/{min_games} games",
        bot=bot_name,
        games=games,
        min_games=min_games,
        rd=round(cached_rd, 2) if cached_rd is not None else None,
        elapsed_sec=round(time.time() - start, 2),
        timeout_sec=timeout,
    )
    return False


# ──────────────────────────────────────────────
# Git Helpers
# ──────────────────────────────────────────────

def _git(*args, check=True):
    """Run git command, return stdout."""
    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True,
            timeout=30
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {args[0]}: timed out after 30s")
    if check and result.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_explicit_presence(*args):
    """Return a destructive Git predicate only for rc=0/explicit rc=1."""

    try:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {args[0]}: timed out after 30s") from exc
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise RuntimeError(
        f"git {args[0]} unavailable (rc={result.returncode}): "
        f"{result.stderr.strip()}"
    )


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "on"}


def evolution_git_push_enabled() -> bool:
    """Return whether evolution-owned commits should be pushed immediately."""
    return _env_flag("EVOLUTION_GIT_PUSH", False)


def evolution_git_push_required() -> bool:
    """Return whether a new generation requires a synchronized remote baseline."""
    return _env_flag("POK_REQUIRE_EVOLUTION_PUSH", _env_flag("POK_EVOLUTION_RUNTIME", False))


def git_publish_status() -> dict:
    """Return branch publication state relative to the configured upstream."""
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    head = _git("rev-parse", "--short=12", "HEAD", check=False).strip()
    upstream = _git(
        "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False
    ).strip() or "origin/main"
    upstream_head = _git("rev-parse", "--short=12", upstream, check=False).strip()
    if not upstream_head:
        return {
            "ok": False,
            "reason": "upstream_missing",
            "branch": branch,
            "head": head,
            "upstream": upstream,
            "upstream_head": "",
            "ahead": None,
            "behind": None,
        }
    raw_counts = _git("rev-list", "--left-right", "--count", f"HEAD...{upstream}", check=False)
    parts = (raw_counts or "").split()
    if len(parts) != 2:
        return {
            "ok": False,
            "reason": "ahead_behind_unavailable",
            "branch": branch,
            "head": head,
            "upstream": upstream,
            "upstream_head": upstream_head,
            "ahead": None,
            "behind": None,
            "raw_counts": raw_counts,
        }
    try:
        ahead, behind = int(parts[0]), int(parts[1])
    except ValueError:
        return {
            "ok": False,
            "reason": "ahead_behind_parse_failed",
            "branch": branch,
            "head": head,
            "upstream": upstream,
            "upstream_head": upstream_head,
            "ahead": None,
            "behind": None,
            "raw_counts": raw_counts,
        }
    return {
        "ok": True,
        "branch": branch,
        "head": head,
        "upstream": upstream,
        "upstream_head": upstream_head,
        "ahead": ahead,
        "behind": behind,
    }


def ensure_publish_ready_for_new_generation() -> tuple[bool, dict]:
    """Block new generations when required evolution commits are not published."""
    push_enabled = evolution_git_push_enabled()
    push_required = evolution_git_push_required()
    status = git_publish_status()
    payload = {
        "push_enabled": push_enabled,
        "push_required": push_required,
        **status,
    }
    if not push_required:
        return True, payload
    if not push_enabled:
        payload.update({
            "blocked": True,
            "reason": "evolution_git_push_disabled",
            "directive": (
                "Long-running evolution requires EVOLUTION_GIT_PUSH=1 so bot "
                "commits and tags publish through origin/main."
            ),
        })
        return False, payload
    if not status.get("ok"):
        payload.update({
            "blocked": True,
            "reason": status.get("reason") or "publish_status_unavailable",
            "directive": "Cannot verify origin/main synchronization before starting a new generation.",
        })
        return False, payload
    if int(status.get("ahead") or 0) > 0:
        payload.update({
            "blocked": True,
            "reason": "unpublished_local_commits",
            "directive": "Push local evolution commits/tags before starting the next generation.",
        })
        return False, payload
    if int(status.get("behind") or 0) > 0:
        payload.update({
            "blocked": True,
            "reason": "remote_main_ahead",
            "directive": "Fetch and fast-forward or reconcile origin/main before starting the next generation.",
        })
        return False, payload
    return True, payload


def git_push_refs(*refs: str) -> bool:
    """Push refs to origin and return the real aggregate result.

    If origin/main advanced with evaluation-contract-neutral changes, reconcile
    by merging origin/main and retrying the push once.
    """
    if not refs:
        return True
    checkpoint = read_pipeline_checkpoint()
    candidate_v = None
    source_v = None
    if isinstance(checkpoint, dict):
        candidate_v = checkpoint.get("next_v")
        source_v = checkpoint.get("source_v")
    if candidate_v is None:
        for ref in refs:
            parsed = parse_tag_version(ref) if isinstance(ref, str) else None
            if parsed is not None:
                candidate_v = parsed
                break

    def _log_event(event_type, severity, message, data):
        try:
            from system_log import log_system_event
            log_system_event(event_type, severity, message, data)
        except Exception:
            pass

    result = reconcile_push_refs(
        tuple(refs),
        root=PROJECT_ROOT,
        git=_git,
        checkpoint=checkpoint if isinstance(checkpoint, dict) else None,
        candidate_v=candidate_v,
        source_v=source_v,
        log_event=_log_event,
    )
    ok = bool(result.get("ok"))
    errors = list(result.get("errors") or [])
    try:
        from system_log import log_system_event
        log_system_event(
            "repo.git_push_done" if ok else "repo.git_push_failed",
            "success" if ok else "error",
            (
                f"Git push succeeded for {', '.join(refs)}"
                if ok else
                f"Git push failed for {', '.join(item['ref'] for item in errors)}"
            ),
            {"refs": list(refs), "ok": ok, **result},
        )
    except Exception:
        pass
    return ok


def _git_ensure_main_branch():
    """Require the canonical evolution branch before an evolution commit.

    Do not stash/pop changes across branches here. Moving a dirty worktree from
    an accidental side branch onto main can mix unrelated edits into evolution
    commits. Runtime guard paths should stop the generation earlier; this is
    the final mutation boundary.
    """
    current = _git("rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    if current == EVOLUTION_BRANCH:
        return
    try:
        from system_log import log_system_event
        log_system_event(
            "repo.branch_commit_blocked",
            "error",
            f"Evolution commit blocked on non-{EVOLUTION_BRANCH} branch: {current}",
            {"current_branch": current, "target_branch": EVOLUTION_BRANCH},
        )
    except Exception:
        pass
    raise RuntimeError(
        f"Refusing evolution commit on branch '{current}'; expected '{EVOLUTION_BRANCH}'."
    )


def git_has_tag(version):
    """Check for a strict-policy completion tag.

    Pre-policy tags are audit/version-authority records only.
    """
    if int(version) < FIRST_STRICT_POLICY_VERSION:
        return False
    return bool(_git("tag", "-l", bot_tag(version), check=False).strip())


def git_has_publication_ref(version):
    """Return whether either create-only publication tag already exists.

    This is a preservation predicate, not completion authority: an interrupted
    publication may have only the high-water tag.  Cleanup paths must retain
    candidate bytes for either partial ref instead of relying on the usual
    tracked-tree or ``.completed`` implications of a finished publication.
    """

    target = int(version)
    names = (bot_tag(target), f"national-high-water-v{target}")
    return any(
        _git_explicit_presence(
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/tags/{name}",
        )
        for name in names
    )


def git_dir_is_committed(version):
    """True if the active bot directory has any git-tracked file.

    Detects BARE COMMITS — code that landed in git via a direct `git commit`
    (e.g. an LLM running git in Bash) but was never finalized through commit_bot,
    so it lacks both the active-epoch tag and the .completed sentinel. This is the
    root-cause signal of the v117 repeated-regeneration loop (2026-06-18): v117
    was bare-committed twice (f6bcccf/f6c4eb7) without a tag, so find_current_v()
    kept returning 116 and the orchestrator regenerated v117 five times until
    commit_bot finally tagged it (20db34c, 22:02). 'git ls-files' is the test:
    a directory with on-disk files but no tracked files is an untracked scratch
    dir (safe to overwrite); a directory with tracked files is committed state.
    """
    return _git_explicit_presence(
        "ls-files",
        "--error-unmatch",
        "--",
        bot_relpath(version) + "/",
    )


def find_max_committed_v():
    """Diagnostic max version whose strict receipt is git-tracked.

    This legacy diagnostic can expose an untagged direct commit for operator
    reconciliation, but it is never completion authority and is never an input
    to version allocation.  Only annotated completion/high-water tags advance
    the published namespace; only checkpoint-bound abandonment receipts may
    reserve a consumed label inside the current epoch.

    Implementation: a SINGLE `git ls-files bots/{active prefix}*` call (not one
    subprocess per directory) keeps this O(1 git call)/generation regardless
    of how many direct strict bot artifacts exist.
    """
    try:
        out = _git("ls-files", "--", f"bots/{active_bot_glob()}", check=False)
    except Exception:
        return 0
    max_v = ARCHIVED_VERSION_HIGH_WATER
    for line in out.splitlines():
        # line like "bots/national_v001/card_utils.py" — extract the dir version
        parts = line.split("/")
        if len(parts) < 2 or not parts[1].startswith(ACTIVE_BOT_PREFIX):
            continue
        v = parse_bot_version(parts[1])
        if v is None or v < FIRST_STRICT_POLICY_VERSION:
            continue
        # A tracked strict epoch receipt is the minimum proof that this is a
        # consumed policy version rather than unrelated/stale source debris.
        receipt_path = f"bots/{bot_name(v)}/policy_epoch_receipt.json"
        if not _git("ls-files", "--error-unmatch", receipt_path, check=False).strip():
            continue
        if v > max_v:
            max_v = v
    return max_v


def _abandoned_receipt_digest(payload):
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AbandonedVersionLedgerError(
            f"abandon receipt is not canonical JSON: {type(exc).__name__}"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _canonical_abandon_json_bytes(payload, *, label):
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AbandonedVersionLedgerError(
            f"{label} is not canonical JSON: {type(exc).__name__}"
        ) from exc


def _validate_abandon_receipt_bounded_fields(reason, infra_failure):
    if not isinstance(reason, str) or not reason.strip():
        raise AbandonedVersionLedgerError("abandon receipt reason is empty")
    if len(reason.encode("utf-8")) > _ABANDONED_VERSION_REASON_MAX_BYTES:
        raise AbandonedVersionLedgerError("abandon receipt reason exceeds byte limit")
    if infra_failure is not None and not isinstance(infra_failure, dict):
        raise AbandonedVersionLedgerError(
            "abandon receipt infra_failure must be an object or null"
        )
    if infra_failure is not None and len(
        _canonical_abandon_json_bytes(
            infra_failure,
            label="abandon receipt infra_failure",
        )
    ) > _ABANDONED_VERSION_INFRA_FAILURE_MAX_BYTES:
        raise AbandonedVersionLedgerError(
            "abandon receipt infra_failure exceeds byte limit"
        )


def _abandoned_checkpoint_envelope(checkpoint):
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    audit = checkpoint.get("audit_context")
    audit = audit if isinstance(audit, dict) else {}
    protocol_bootstrap = audit.get("protocol_bootstrap")
    return {
        "checkpoint_schema_version": checkpoint.get("checkpoint_schema_version"),
        "evaluation_epoch": checkpoint.get("evaluation_epoch"),
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "parent2_v": checkpoint.get("parent2_v"),
        "generation_mode": checkpoint.get("generation_mode"),
        "epoch_binding": checkpoint.get("epoch_binding"),
        "audit_context": (
            {"protocol_bootstrap": protocol_bootstrap}
            if protocol_bootstrap is not None
            else {}
        ),
    }


def _validate_abandoned_checkpoint(checkpoint, *, project_root):
    from checkpoint_schema import (
        checkpoint_epoch_errors,
        live_checkpoint_parent_authority_errors,
        live_policy_epoch_reset_receipt_errors,
    )

    errors = list(checkpoint_epoch_errors(checkpoint))
    if not errors:
        errors.extend(
            live_checkpoint_parent_authority_errors(
                checkpoint,
                repo_root=project_root,
            )
        )
    if not errors:
        errors.extend(
            live_policy_epoch_reset_receipt_errors(
                checkpoint,
                project_root=project_root,
            )
        )
    stage = checkpoint.get("stage") if isinstance(checkpoint, dict) else None
    if not isinstance(stage, str) or not stage.strip():
        errors.append("abandon_checkpoint_stage_missing")
    # ``timed_out`` is an active terminalization lease: the only legal next
    # route is the recorded canonical-abandon transaction, which appends this
    # receipt before quarantining bytes and clearing the exact checkpoint.
    # Infra timeouts preserve the candidate for precommit retry, while archived
    # and abandoned states have already crossed their terminal boundary.
    elif stage in {"infra_timed_out", "archived", "abandoned"}:
        errors.append("abandon_checkpoint_stage_not_active")
    workflow_run_id = (
        checkpoint.get("workflow_run_id") if isinstance(checkpoint, dict) else None
    )
    if not isinstance(workflow_run_id, str) or not workflow_run_id.strip():
        errors.append("abandon_checkpoint_workflow_run_id_missing")
    elif checkpoint.get("next_v") == FIRST_STRICT_POLICY_VERSION and re.fullmatch(
        rf"generation:{int(checkpoint['next_v'])}:workflow-v[1-9][0-9]*",
        workflow_run_id,
    ) is None:
        errors.append("abandon_checkpoint_workflow_run_id_invalid")
    revision = (
        checkpoint.get("checkpoint_revision") if isinstance(checkpoint, dict) else None
    )
    if type(revision) is not int or revision < 1:
        errors.append("abandon_checkpoint_revision_invalid")
    if errors:
        raise AbandonedVersionLedgerError(
            "abandon checkpoint is not current-epoch bound: "
            + "; ".join(dict.fromkeys(map(str, errors)))
        )


def _build_abandoned_version_receipt(
    checkpoint,
    *,
    reason,
    infra_failure=None,
    timestamp=None,
    previous_receipt_digest=None,
    project_root=None,
):
    project_root = Path(project_root) if project_root is not None else PROJECT_ROOT
    _validate_abandoned_checkpoint(checkpoint, project_root=project_root)
    reason = str(reason or "").strip()
    _validate_abandon_receipt_bounded_fields(reason, infra_failure)
    timestamp = time.time() if timestamp is None else timestamp
    if (
        type(timestamp) not in {int, float}
        or timestamp != timestamp
        or timestamp < 0
        or timestamp > 100_000_000_000
    ):
        raise AbandonedVersionLedgerError("abandon receipt timestamp is invalid")
    if previous_receipt_digest is not None and not re.fullmatch(
        r"[0-9a-f]{64}", str(previous_receipt_digest)
    ):
        raise AbandonedVersionLedgerError(
            "abandon receipt previous digest is invalid"
        )
    payload = {
        "schema_version": ABANDONED_VERSION_RECEIPT_SCHEMA_VERSION,
        "kind": ABANDONED_VERSION_RECEIPT_KIND,
        "evaluation_epoch": EVALUATION_EPOCH,
        "version": checkpoint["next_v"],
        "source_v": checkpoint["source_v"],
        "checkpoint_stage": checkpoint["stage"],
        "workflow_run_id": checkpoint["workflow_run_id"],
        "checkpoint_revision": checkpoint["checkpoint_revision"],
        "checkpoint_envelope": _abandoned_checkpoint_envelope(checkpoint),
        "reason": reason,
        "timestamp": timestamp,
        "infra_failure": infra_failure,
        "previous_receipt_digest": previous_receipt_digest,
    }
    return {**payload, "receipt_digest": _abandoned_receipt_digest(payload)}


def _abandoned_version_receipt_errors(
    receipt,
    *,
    expected_previous_digest,
    project_root,
):
    errors = []
    if not isinstance(receipt, dict):
        return ["abandon_receipt_not_object"]
    if set(receipt) != _ABANDONED_VERSION_RECEIPT_KEYS:
        errors.append("abandon_receipt_fields_mismatch")
    if receipt.get("schema_version") != ABANDONED_VERSION_RECEIPT_SCHEMA_VERSION:
        errors.append("abandon_receipt_schema_mismatch")
    if receipt.get("kind") != ABANDONED_VERSION_RECEIPT_KIND:
        errors.append("abandon_receipt_kind_mismatch")
    if receipt.get("evaluation_epoch") != EVALUATION_EPOCH:
        errors.append("abandon_receipt_epoch_mismatch")
    version = receipt.get("version")
    source_v = receipt.get("source_v")
    if type(version) is not int or version < FIRST_STRICT_POLICY_VERSION:
        errors.append("abandon_receipt_version_invalid")
    if type(source_v) is not int:
        errors.append("abandon_receipt_source_version_invalid")
    if receipt.get("previous_receipt_digest") != expected_previous_digest:
        errors.append("abandon_receipt_chain_mismatch")
    timestamp = receipt.get("timestamp")
    if (
        type(timestamp) not in {int, float}
        or timestamp != timestamp
        or timestamp < 0
        or timestamp > 100_000_000_000
    ):
        errors.append("abandon_receipt_timestamp_invalid")
    try:
        _validate_abandon_receipt_bounded_fields(
            receipt.get("reason"),
            receipt.get("infra_failure"),
        )
    except AbandonedVersionLedgerError as exc:
        errors.append(str(exc).replace(" ", "_"))
    recorded_digest = receipt.get("receipt_digest")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    try:
        expected_digest = _abandoned_receipt_digest(unsigned)
    except AbandonedVersionLedgerError:
        expected_digest = ""
    if (
        not isinstance(recorded_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", recorded_digest)
        or recorded_digest != expected_digest
    ):
        errors.append("abandon_receipt_digest_mismatch")

    envelope = receipt.get("checkpoint_envelope")
    if not isinstance(envelope, dict):
        errors.append("abandon_receipt_checkpoint_envelope_not_object")
    else:
        if set(envelope) != _ABANDONED_CHECKPOINT_ENVELOPE_KEYS:
            errors.append("abandon_receipt_checkpoint_envelope_fields_mismatch")
        checkpoint = {
            **envelope,
            "stage": receipt.get("checkpoint_stage"),
            "workflow_run_id": receipt.get("workflow_run_id"),
            "checkpoint_revision": receipt.get("checkpoint_revision"),
        }
        try:
            _validate_abandoned_checkpoint(
                checkpoint,
                project_root=project_root,
            )
        except AbandonedVersionLedgerError as exc:
            errors.append(str(exc))
        if envelope.get("next_v") != version:
            errors.append("abandon_receipt_checkpoint_version_mismatch")
        if envelope.get("source_v") != source_v:
            errors.append("abandon_receipt_checkpoint_source_mismatch")
    return list(dict.fromkeys(errors))


def _decode_abandoned_version_receipts(
    raw,
    *,
    allow_empty,
    project_root,
):
    if not isinstance(raw, str):
        raise AbandonedVersionLedgerError("abandon receipt ledger is not text")
    if len(raw.encode("utf-8")) > _ABANDONED_VERSION_LEDGER_MAX_BYTES:
        raise AbandonedVersionLedgerError("abandon receipt ledger exceeds byte limit")
    if not raw:
        if allow_empty:
            return []
        raise AbandonedVersionLedgerError("abandon receipt ledger is empty")
    if not raw.endswith("\n"):
        raise AbandonedVersionLedgerError(
            "abandon receipt ledger has an incomplete final row"
        )
    receipts = []
    previous_digest = None
    previous_version = None
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            raise AbandonedVersionLedgerError(
                f"abandon receipt ledger blank row at line {line_number}"
            )
        try:
            receipt = json.loads(line)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise AbandonedVersionLedgerError(
                f"abandon receipt ledger malformed at line {line_number}: "
                f"{type(exc).__name__}"
            ) from exc
        errors = _abandoned_version_receipt_errors(
            receipt,
            expected_previous_digest=previous_digest,
            project_root=project_root,
        )
        if errors:
            raise AbandonedVersionLedgerError(
                f"abandon receipt ledger invalid at line {line_number}: "
                + "; ".join(errors)
            )
        version = receipt["version"]
        if previous_version is not None and version < previous_version:
            raise AbandonedVersionLedgerError(
                f"abandon receipt version order regressed at line {line_number}"
            )
        receipts.append(receipt)
        previous_digest = receipt["receipt_digest"]
        previous_version = version
    return receipts


def load_abandoned_version_receipts(*, path=None, project_root=None):
    """Load the exact current-epoch receipt chain or raise on any ambiguity."""

    path = Path(path) if path is not None else Path(ABANDONED_VERSIONS_FILE)
    project_root = Path(project_root) if project_root is not None else PROJECT_ROOT
    if not os.path.lexists(path):
        return []
    try:
        with _locked_state_sidecar(path, lock_type=fcntl.LOCK_SH):
            raw = _read_regular_state_text(path, allow_missing=False)
            if len(raw.encode("utf-8")) > _ABANDONED_VERSION_LEDGER_MAX_BYTES:
                raise AbandonedVersionLedgerError(
                    "abandon receipt ledger exceeds byte limit"
                )
    except AbandonedVersionLedgerError:
        raise
    except (OSError, UnicodeError) as exc:
        raise AbandonedVersionLedgerError(
            f"abandon receipt ledger unreadable: {type(exc).__name__}: {exc}"
        ) from exc
    return _decode_abandoned_version_receipts(
        raw,
        allow_empty=False,
        project_root=project_root,
    )


def _abandon_authority_from_receipts(
    receipts,
    *,
    published_high_water,
    retryable_first_strict,
):
    versions = [
        int(receipt["version"])
        for receipt in receipts
        if not (
            retryable_first_strict
            and int(receipt["version"]) == FIRST_STRICT_POLICY_VERSION
        )
    ]
    return {
        "published_high_water": int(published_high_water),
        "floor": max(versions, default=0),
        "head_digest": receipts[-1]["receipt_digest"] if receipts else None,
        "receipt_count": len(receipts),
    }


def abandoned_version_authority(
    *,
    initialization=None,
    published_high_water=None,
    path=None,
    project_root=None,
):
    """Return one validated ledger snapshot (floor and chain head together)."""

    from epoch_authority import policy_epoch_initialization

    if published_high_water is None:
        published_high_water = find_current_v()
    published_high_water = int(published_high_water)
    if initialization is None:
        initialization = policy_epoch_initialization(current_v=published_high_water)
    if not isinstance(initialization, dict) or not initialization.get("initialized"):
        return {
            "published_high_water": published_high_water,
            "floor": 0,
            "head_digest": None,
            "receipt_count": 0,
        }
    receipts = load_abandoned_version_receipts(
        path=path,
        project_root=project_root,
    )
    retryable_first_strict = bool(
        initialization.get("state") == "fresh_bootstrap_ready"
        and initialization.get("strict_published") is False
    )
    return _abandon_authority_from_receipts(
        receipts,
        published_high_water=published_high_water,
        retryable_first_strict=retryable_first_strict,
    )


def checkpoint_allocation_authority(*, expected_next_v=None):
    """Resolve system authority and optionally assert one requested successor."""

    from epoch_authority import policy_epoch_initialization

    published = int(find_current_v())
    initialization = policy_epoch_initialization(current_v=published)
    if not initialization.get("initialized"):
        raise AbandonedVersionLedgerError(
            "checkpoint allocation requires an initialized policy epoch"
        )
    abandoned = abandoned_version_authority(
        initialization=initialization,
        published_high_water=published,
    )
    authority = {
        "published_high_water": published,
        "abandoned_receipt_floor": int(abandoned["floor"]),
        "abandoned_receipt_head_digest": abandoned["head_digest"],
        "allocation_floor": max(published, int(abandoned["floor"])),
    }
    if expected_next_v is not None and (
        type(expected_next_v) is not int
        or expected_next_v != authority["allocation_floor"] + 1
    ):
        raise AbandonedVersionLedgerError(
            "checkpoint target is not the live allocation successor"
        )
    return authority


def _receipt_identity_matches_checkpoint(receipt, checkpoint):
    return bool(
        isinstance(receipt, dict)
        and receipt.get("workflow_run_id") == checkpoint.get("workflow_run_id")
        and receipt.get("checkpoint_revision")
        == checkpoint.get("checkpoint_revision")
        and receipt.get("checkpoint_stage") == checkpoint.get("stage")
        and receipt.get("checkpoint_envelope")
        == _abandoned_checkpoint_envelope(checkpoint)
    )


def recorded_abandon_receipt_for_checkpoint(
    checkpoint,
    *,
    path=None,
    project_root=None,
):
    """Return the sole durable terminal receipt for an uncleared checkpoint."""

    receipts = load_abandoned_version_receipts(
        path=path,
        project_root=project_root,
    )
    matches = [
        receipt
        for receipt in receipts
        if _receipt_identity_matches_checkpoint(receipt, checkpoint)
    ]
    if not matches:
        return None
    if len(matches) != 1 or matches[0] is not receipts[-1]:
        raise AbandonedVersionLedgerError(
            "recorded abandon checkpoint identity is not the unique chain head"
        )
    return dict(matches[0])


def append_abandoned_version_receipt(
    checkpoint,
    *,
    reason,
    infra_failure=None,
    timestamp=None,
    path=None,
    project_root=None,
):
    """Append one checkpoint-bound receipt after validating the whole chain."""

    path = Path(path) if path is not None else Path(ABANDONED_VERSIONS_FILE)
    project_root = Path(project_root) if project_root is not None else PROJECT_ROOT
    # Validate before opening in append mode so an unauthorized call cannot
    # manufacture even an empty ledger claim.
    _validate_abandoned_checkpoint(checkpoint, project_root=project_root)
    normalized_reason = str(reason or "").strip()
    _validate_abandon_receipt_bounded_fields(normalized_reason, infra_failure)
    try:
        with _locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
            existed = os.path.lexists(path)
            raw = _read_regular_state_text(path, allow_missing=True)
            if existed and not raw:
                raise AbandonedVersionLedgerError(
                    "abandon receipt ledger is empty"
                )
            receipts = _decode_abandoned_version_receipts(
                raw,
                allow_empty=not existed,
                project_root=project_root,
            )
            matching = [
                receipt
                for receipt in receipts
                if _receipt_identity_matches_checkpoint(receipt, checkpoint)
            ]
            if matching:
                if len(matching) != 1 or matching[0] is not receipts[-1]:
                    raise AbandonedVersionLedgerError(
                        "abandon receipt identity is not the unique chain head"
                    )
                prior = matching[0]
                if (
                    prior.get("reason") != normalized_reason
                    or prior.get("infra_failure") != infra_failure
                ):
                    raise AbandonedVersionLedgerError(
                        "abandon receipt identity already exists with different payload"
                    )
                # Clear/fsync failure may replay the exact terminal command.
                # Re-prove both the published inode and its parent.  A prior
                # atomic replace may have succeeded before the directory fsync
                # raised, so returning the row without this retry could permit
                # candidate/checkpoint destruction on a non-durable receipt.
                _fsync_regular_state_file_and_parent(path)
                return dict(prior)
            previous_digest = (
                receipts[-1]["receipt_digest"] if receipts else None
            )
            published = int(find_current_v())
            retryable_first_strict = published < FIRST_STRICT_POLICY_VERSION
            authority = _abandon_authority_from_receipts(
                receipts,
                published_high_water=published,
                retryable_first_strict=retryable_first_strict,
            )
            from checkpoint_schema import live_checkpoint_allocation_authority_errors

            live_errors = live_checkpoint_allocation_authority_errors(
                checkpoint,
                published_high_water=published,
                abandoned_receipt_floor=int(authority["floor"]),
                abandoned_receipt_head_digest=authority["head_digest"],
            )
            if live_errors:
                raise AbandonedVersionLedgerError(
                    "abandon checkpoint allocation authority changed: "
                    + "; ".join(live_errors)
                )
            receipt = _build_abandoned_version_receipt(
                checkpoint,
                reason=normalized_reason,
                infra_failure=infra_failure,
                timestamp=timestamp,
                previous_receipt_digest=previous_digest,
                project_root=project_root,
            )
            if receipts and receipt["version"] < receipts[-1]["version"]:
                raise AbandonedVersionLedgerError(
                    "abandon receipt version would regress the durable chain"
                )
            encoded_row = _canonical_abandon_json_bytes(
                receipt,
                label="abandon receipt",
            ) + b"\n"
            final_bytes = raw.encode("utf-8") + encoded_row
            if len(final_bytes) > _ABANDONED_VERSION_LEDGER_MAX_BYTES:
                raise AbandonedVersionLedgerError(
                    "abandon receipt ledger would exceed byte limit"
                )
            _atomic_publish_state_text(
                path,
                final_bytes.decode("utf-8"),
            )
        return receipt
    except AbandonedVersionLedgerError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise AbandonedVersionLedgerError(
            f"abandon receipt append failed: {type(exc).__name__}: {exc}"
        ) from exc


def find_abandoned_version_floor():
    """Return the content-bound allocation floor for this initialized epoch.

    A retired/pre-reset file is ignored because it has no epoch authority.  Once
    the epoch is initialized, however, every row must be a valid digest-chained
    receipt created from a valid active checkpoint; unreadable, legacy, partial
    or tampered state raises and therefore blocks allocation.
    """

    try:
        return int(abandoned_version_authority()["floor"])
    except AbandonedVersionLedgerError:
        raise
    except Exception as exc:
        raise AbandonedVersionLedgerError(
            "policy epoch initialization unavailable while reading abandon receipts: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def abandoned_version_attempt_count(version):
    """Return the greatest validated workflow attempt for a reusable label."""

    target = int(version)
    attempts = []
    pattern = re.compile(
        rf"^generation:{re.escape(str(target))}:workflow-v([1-9][0-9]*)$"
    )
    for receipt in load_abandoned_version_receipts():
        if receipt["version"] != target:
            continue
        match = pattern.fullmatch(str(receipt.get("workflow_run_id") or ""))
        if not match:
            raise AbandonedVersionLedgerError(
                f"abandon receipt workflow attempt is invalid for v{target}"
            )
        attempts.append(int(match.group(1)))
    return max(attempts, default=0)


def compute_next_generation_v(current_v=None, max_committed_v=None, abandoned_floor=None):
    """Return the next free current-epoch label from authoritative inputs only.

    ``max_committed_v`` remains accepted for call-site compatibility but is
    intentionally ignored.  A bare commit is operator-reconciliation evidence,
    never published high-water or an allocation receipt.
    """

    del max_committed_v
    if current_v is None:
        current_v = find_current_v()
    if abandoned_floor is None:
        abandoned_floor = find_abandoned_version_floor()
    return max(int(current_v), int(abandoned_floor)) + 1


def publish_runtime_expected_head(reason: str = "", version=None) -> str:
    """Publish the current HEAD as the validated runtime baseline.

    The background runtime guard owns the final stop/continue decision, but
    pipeline-owned commits must be able to tell it that the new HEAD is expected
    instead of being an external drift.
    """
    head = _git("rev-parse", "--short=12", "HEAD", check=False).strip()
    if not head:
        return ""
    previous = os.environ.get("POK_RUNTIME_EXPECTED_HEAD", "").strip()
    os.environ["POK_RUNTIME_EXPECTED_HEAD"] = head
    if previous != head:
        try:
            from system_log import log_system_event
            log_system_event(
                "repo.runtime_expected_head_published",
                "info",
                f"Published runtime expected HEAD {previous or '<none>'} -> {head}",
                {
                    "previous_expected_head": previous,
                    "expected_head": head,
                    "reason": reason,
                    "version": version,
                },
            )
        except Exception:
            pass
    return head


def _require_national_epoch_registry_for_commit():
    from national_epoch_registry import load_registry_state

    state = load_registry_state(
        PROJECT_ROOT,
        legacy_ledger=REAPED_BOTS_FILE,
        include_history=True,
    )
    if not state.available or not state.migration_marker:
        diagnostics = "; ".join(state.diagnostics) or "migration marker missing"
        raise RuntimeError(
            "national epoch registry is not durably migrated; " + diagnostics
        )
    return state


def _advance_national_epoch_high_water(version):
    from national_epoch_registry import advance_high_water

    return advance_high_water(
        int(version),
        repo_root=PROJECT_ROOT,
        legacy_ledger=REAPED_BOTS_FILE,
    )


def git_commit_bot(
    version,
    source_v,
    strategy_tag,
    rating_info="",
    parent2_v=None,
    *,
    official_certificate,
):
    """Commit a completed bot generation.

    Always commits on EVOLUTION_BRANCH (main). Calls _git_ensure_main_branch()
    first so that LLM-created side-branches never pollute the evolution history.
    Stage only the evolved bot and curated learning notes; daemon/result churn
    must not leak into evolution commits.
    """
    certificate = dict(official_certificate or {})
    certificate_digest = str(certificate.get("certificate_digest") or "")
    expected_bot_hash = str(certificate.get("candidate_hash") or "")
    certificate_policy = str(certificate.get("policy_id") or "")
    if not certificate:
        raise RuntimeError(
            "official full certificate is required for national-bot commit/tag"
        )
    if not (certificate_digest and expected_bot_hash and certificate_policy):
        raise RuntimeError("official certificate metadata is incomplete")
    # Import lazily so the foundational infrastructure module does not create a
    # module-load cycle with official certification.  The policy identifier has
    # one owner; commit/tag code must not drift from certificate issuance.
    from official_certification import FULL_POLICY_ID

    if certificate_policy != FULL_POLICY_ID:
        raise RuntimeError(
            f"unsupported official certificate policy: {certificate_policy or '<missing>'}"
        )

    from bot_artifact import hash_path, validate_staged_artifact

    current_bot_hash = hash_path(get_bot_dir(version))
    if current_bot_hash != expected_bot_hash:
        raise RuntimeError(
            "candidate changed after official certification: "
            f"expected {expected_bot_hash}, current {current_bot_hash}"
        )

    _require_national_epoch_registry_for_commit()

    _git_ensure_main_branch()
    parent_line = f"parent: {bot_name(source_v)}"
    if parent2_v is not None:
        parent_line += f"\nparent2: {bot_name(parent2_v)}"
    msg = (
        f"evolve: v{source_v} → v{version}\n\n"
        f"{parent_line}\n"
        f"strategy: {strategy_tag}\n"
        f"{rating_info}"
    )
    msg += (
        f"\nofficial-certificate: {certificate_digest}"
        f"\nofficial-candidate-hash: {expected_bot_hash}"
        f"\nofficial-policy: {certificate_policy}"
    )
    bot_path = bot_relpath(version)
    preexisting_staged = [
        p for p in _git("diff", "--cached", "--name-only", check=False).splitlines()
        if p
    ]
    preexisting_scope = classify_status_entries(
        [f"?? {path}" for path in preexisting_staged],
        int(version),
    )
    preexisting_blocking = (
        list(preexisting_scope.get("critical_entries") or [])
        + list(preexisting_scope.get("foreign_bot_entries") or [])
    )
    if preexisting_blocking:
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.git_commit_blocked_preexisting_staged",
                "error",
                f"v{version}: refusing commit because blocking staged files already exist",
                {
                    "version": version,
                    "staged_files": preexisting_staged[:40],
                    "blocking_staged": preexisting_blocking[:40],
                },
            )
        except Exception:
            pass
        raise RuntimeError(
            "Refusing git_commit_bot with pre-existing blocking staged files: "
            + ", ".join(preexisting_blocking[:10])
        )

    from official_certification import publish_certificate_attestation

    publication = publish_certificate_attestation(certificate, get_bot_dir(version))
    if publication.get("certificate_digest") != certificate_digest:
        raise RuntimeError("published official attestation changed certificate digest")
    certificate_path = str(publication.get("relative_path") or "")
    if not certificate_path:
        raise RuntimeError("published official attestation path is missing")

    # LOG GAP FIX (2026-06-29): record what gets staged so a hand-edit bypass
    # (orchestrator LLM mutating bot code outside execute_workers) is visible.
    _staged = _git("add", "--", bot_path, certificate_path, check=False)
    staged_bot_hash = hash_path(get_bot_dir(version))
    if staged_bot_hash != expected_bot_hash:
        _git("restore", "--staged", "--", bot_path, certificate_path, check=False)
        raise RuntimeError(
            "candidate changed while staging official-certified artifact: "
            f"expected {expected_bot_hash}, current {staged_bot_hash}"
        )
    try:
        staged_artifact = validate_staged_artifact(
            get_bot_dir(version),
            repo_root=PROJECT_ROOT,
        )
    except Exception as exc:
        _git("restore", "--staged", "--", bot_path, certificate_path, check=False)
        raise RuntimeError(
            "staged bot artifact validation failed: "
            f"{type(exc).__name__}: {str(exc)[:500]}"
        ) from exc
    if (
        not staged_artifact.get("valid")
        or staged_artifact.get("working_hash") != expected_bot_hash
        or staged_artifact.get("staged_hash") != expected_bot_hash
    ):
        _git("restore", "--staged", "--", bot_path, certificate_path, check=False)
        working_files = {
            str(item.get("path") or "")
            for item in (staged_artifact.get("working_manifest") or {}).get("entries") or []
            if item.get("type") == "file"
        }
        staged_files = {
            str(item.get("path") or "")
            for item in (staged_artifact.get("staged_manifest") or {}).get("entries") or []
            if item.get("type") == "file"
        }
        raise RuntimeError(
            "staged Git blobs do not reproduce the certified bot artifact: "
            f"working_hash={staged_artifact.get('working_hash')} "
            f"staged_hash={staged_artifact.get('staged_hash')} "
            f"missing={sorted(working_files - staged_files)[:10]} "
            f"extra={sorted(staged_files - working_files)[:10]}"
        )
    allowed_paths = [bot_path, certificate_path]
    # Capture the staged file list right before commit for auditability.
    _staged_files = _git("diff", "--cached", "--name-only", check=False).strip().splitlines()
    allowed_exact = set(allowed_paths)
    allowed_prefixes = [p.rstrip("/") + "/" for p in allowed_paths if p.endswith(bot_name(version))]
    commit_staged_files = [
        p for p in _staged_files
        if p in allowed_exact or any(p.startswith(prefix) for prefix in allowed_prefixes)
    ]
    outside_staged = [
        p for p in _staged_files
        if p not in allowed_exact and not any(p.startswith(prefix) for prefix in allowed_prefixes)
    ]
    outside_scope = classify_status_entries([f"?? {path}" for path in outside_staged], int(version))
    unexpected_staged = (
        list(outside_scope.get("critical_entries") or [])
        + list(outside_scope.get("foreign_bot_entries") or [])
    )
    if not commit_staged_files:
        raise RuntimeError(f"Refusing git_commit_bot with no staged files under {bot_path}")
    if certificate_path not in _staged_files:
        for path in allowed_paths:
            _git("restore", "--staged", "--", path, check=False)
        raise RuntimeError(
            f"Refusing git_commit_bot without staged official attestation {certificate_path}"
        )
    if unexpected_staged:
        for path in allowed_paths:
            _git("restore", "--staged", "--", path, check=False)
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.git_commit_blocked_unexpected_staged",
                "error",
                f"v{version}: refusing commit because unrelated staged files appeared",
                {
                    "version": version,
                    "unexpected_staged": unexpected_staged[:40],
                    "allowed_paths": allowed_paths,
                    "outside_staged": outside_staged[:40],
                },
            )
        except Exception:
            pass
        raise RuntimeError(
            "Refusing git_commit_bot with unexpected staged files: "
            + ", ".join(unexpected_staged[:10])
        )
    try:
        from system_log import log_system_event
        log_system_event(
            "pipeline.git_commit_staged", "info",
            f"v{version}: staging {len(commit_staged_files)} file(s) for commit",
            {"version": version, "source_v": source_v,
             "staged_files": commit_staged_files[:30],
             "external_staged_preserved": outside_staged[:30]},
        )
    except Exception:
        pass
    _git("commit", "-m", msg, "--", *allowed_paths)
    _commit_hash = _git("rev-parse", "HEAD", check=False).strip()[:12]
    publish_runtime_expected_head("bot_commit", version=version)
    high_water_mutation = _advance_national_epoch_high_water(version)
    high_water_refs = list(high_water_mutation.created_tags)
    tag = bot_tag(version)
    if _git("tag", "-l", tag, check=False).strip():
        raise RuntimeError(
            f"Refusing to delete or recreate immutable completion tag {tag}; "
            "resume through the durable publication transaction"
        )
    tag_message = f"National bot v{format_version(version)}: {strategy_tag}"
    tag_message += (
        f"\n\nofficial-certificate: {certificate_digest}"
        f"\nofficial-candidate-hash: {expected_bot_hash}"
        f"\nofficial-policy: {certificate_policy}"
    )
    _git("tag", "-a", tag, "HEAD", "-m", tag_message)
    from bot_artifact import validate_completion_tag

    tag_validation = validate_completion_tag(
        get_bot_dir(version),
        expected_metadata={
            "official-certificate": certificate_digest,
            "official-candidate-hash": expected_bot_hash,
            "official-policy": certificate_policy,
        },
        certificate_path=certificate_path,
    )
    if not tag_validation.get("valid"):
        raise RuntimeError(
            "new completion tag failed structural validation: "
            + ", ".join(tag_validation.get("issues") or [])
        )
    try:
        from official_eligibility import clear_registry_state_cache

        clear_registry_state_cache()
    except Exception:
        pass
    try:
        from system_log import log_system_event
        log_system_event(
            "pipeline.git_commit_done", "success",
            f"v{version}: committed {_commit_hash} + tag {tag}",
            {"version": version, "commit_hash": _commit_hash, "tag": tag},
        )
    except Exception:
        pass

    push_ok = False
    if evolution_git_push_enabled() or evolution_git_push_required():
        push_ok = git_push_refs("main", tag, *high_water_refs)
        publish_runtime_expected_head("bot_commit_push", version=version)
    return push_ok


def _git_command_succeeds(*args: str) -> bool:
    """Run a Git predicate while retaining its return code."""

    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _git_blob_bytes(ref: str, relative_path: str) -> bytes:
    result = subprocess.run(
        ["git", "cat-file", "blob", f"{ref}:{relative_path}"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Git blob unavailable at {ref}:{relative_path}: "
            + result.stderr.decode("utf-8", "replace")[:300]
        )
    return bytes(result.stdout)


def _publication_commit_paths(intent: dict) -> tuple[str, str]:
    return (
        bot_relpath(int(intent["version"])),
        str(intent["certificate_relative_path"]),
    )


def _validate_publication_certificate_file(intent: dict) -> None:
    from publication_transaction import file_sha256

    relative = str(intent.get("certificate_relative_path") or "")
    path = PROJECT_ROOT / relative
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("publication certificate attestation is missing or not regular")
    if file_sha256(path) != intent.get("certificate_file_sha256"):
        raise RuntimeError("publication certificate attestation bytes drifted")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"publication certificate attestation is unreadable: {type(exc).__name__}"
        ) from exc
    if payload.get("attestation_digest") != intent.get(
        "certificate_attestation_digest"
    ):
        raise RuntimeError("publication certificate attestation digest drifted")
    if payload.get("certificate_digest") != intent.get(
        "official_certificate_digest"
    ):
        raise RuntimeError("publication certificate digest drifted")


def _validate_existing_publication_commit(intent: dict, commit_oid: str) -> None:
    """Prove a recovered commit is the sole scoped effect after the intent."""

    from bot_artifact import canonical_digest, git_tree_artifact_manifest
    from publication_transaction import file_sha256

    baseline = str(intent.get("baseline_head") or "")
    bot_path, certificate_path = _publication_commit_paths(intent)
    if not _git_command_succeeds(
        "merge-base", "--is-ancestor", baseline, commit_oid
    ):
        raise RuntimeError("publication commit is not descended from intent baseline")
    if not _git_command_succeeds(
        "merge-base", "--is-ancestor", commit_oid, "refs/heads/main"
    ):
        raise RuntimeError("publication commit is not reachable from local main")
    changed = [
        item
        for item in _git(
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            commit_oid,
            check=False,
        ).splitlines()
        if item
    ]
    bot_prefix = bot_path.rstrip("/") + "/"
    if (
        certificate_path not in changed
        or not any(item.startswith(bot_prefix) for item in changed)
        or any(
            item != certificate_path and not item.startswith(bot_prefix)
            for item in changed
        )
    ):
        raise RuntimeError(
            "publication commit changed paths outside candidate/certificate scope: "
            + ", ".join(changed[:20])
        )
    manifest = git_tree_artifact_manifest(
        get_bot_dir(int(intent["version"])),
        commit_oid,
        repo_root=PROJECT_ROOT,
    )
    if canonical_digest(manifest) != intent.get("candidate_artifact_hash"):
        raise RuntimeError("publication commit candidate tree hash mismatch")
    certificate_bytes = _git_blob_bytes(commit_oid, certificate_path)
    if hashlib.sha256(certificate_bytes).hexdigest() != intent.get(
        "certificate_file_sha256"
    ):
        raise RuntimeError("publication commit certificate blob hash mismatch")
    try:
        certificate_payload = json.loads(certificate_bytes.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"publication commit certificate blob is invalid: {type(exc).__name__}"
        ) from exc
    if certificate_payload.get("attestation_digest") != intent.get(
        "certificate_attestation_digest"
    ):
        raise RuntimeError("publication commit attestation digest mismatch")
    if certificate_payload.get("certificate_digest") != intent.get(
        "official_certificate_digest"
    ):
        raise RuntimeError("publication commit official certificate mismatch")
    message = _git("show", "-s", "--format=%B", commit_oid, check=False).strip()
    if message != str(intent.get("commit_message") or "").strip():
        raise RuntimeError("publication commit message does not match frozen intent")


def _resolve_existing_publication_commit(intent: dict) -> str:
    baseline = str(intent.get("baseline_head") or "")
    bot_path, certificate_path = _publication_commit_paths(intent)
    for relative in (bot_path, certificate_path):
        if _git_command_succeeds(
            "cat-file", "-e", f"{baseline}:{relative}"
        ):
            raise RuntimeError(
                f"publication path already existed at intent baseline: {relative}"
            )
    commits = [
        item
        for item in _git(
            "rev-list",
            "--reverse",
            f"{baseline}..refs/heads/main",
            "--",
            bot_path,
            certificate_path,
            check=False,
        ).splitlines()
        if item
    ]
    if len(commits) > 1:
        raise RuntimeError(
            "multiple commits touched frozen publication paths after intent"
        )
    if not commits:
        return ""
    commit_oid = commits[0]
    _validate_existing_publication_commit(intent, commit_oid)
    return commit_oid


def _git_with_index(index_path: Path, *args: str) -> str:
    """Run one Git index operation against a transaction-private index."""

    environment = os.environ.copy()
    environment["GIT_INDEX_FILE"] = str(index_path)
    result = subprocess.run(
        ["git", *args],
        cwd=str(PROJECT_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {args[0]} with publication index failed: "
            + result.stderr.strip()[:500]
        )
    return result.stdout.strip()


def _publication_commit_object(tree_oid: str, parent_oid: str, message: str) -> str:
    """Create an immutable commit object without consulting the worktree."""

    result = subprocess.run(
        ["git", "commit-tree", tree_oid, "-p", parent_oid],
        cwd=str(PROJECT_ROOT),
        input=message,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    commit_oid = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit_oid):
        raise RuntimeError(
            "git commit-tree failed for frozen publication tree: "
            + result.stderr.strip()[:500]
        )
    return commit_oid


def _validate_frozen_publication_tree(
    intent: dict,
    *,
    tree_oid: str,
    parent_oid: str,
) -> None:
    """Prove the private-index tree contains only the frozen publication."""

    from bot_artifact import canonical_digest, git_tree_artifact_manifest

    bot_path, certificate_path = _publication_commit_paths(intent)
    changed = [
        item
        for item in _git(
            "diff",
            "--name-only",
            parent_oid,
            tree_oid,
            "--",
            check=False,
        ).splitlines()
        if item
    ]
    bot_prefix = bot_path.rstrip("/") + "/"
    if (
        certificate_path not in changed
        or not any(item.startswith(bot_prefix) for item in changed)
        or any(
            item != certificate_path and not item.startswith(bot_prefix)
            for item in changed
        )
    ):
        raise RuntimeError(
            "frozen publication tree changed paths outside candidate/certificate scope: "
            + ", ".join(changed[:20])
        )
    manifest = git_tree_artifact_manifest(
        get_bot_dir(int(intent["version"])),
        tree_oid,
        repo_root=PROJECT_ROOT,
    )
    if canonical_digest(manifest) != intent.get("candidate_artifact_hash"):
        raise RuntimeError("private-index candidate tree differs from frozen intent")
    certificate_bytes = _git_blob_bytes(tree_oid, certificate_path)
    if hashlib.sha256(certificate_bytes).hexdigest() != intent.get(
        "certificate_file_sha256"
    ):
        raise RuntimeError("private-index certificate differs from frozen intent")


def _create_publication_commit(intent: dict) -> str:
    """CAS a commit built from an immutable private-index tree onto main."""

    from bot_artifact import hash_path, validate_staged_artifact

    version = int(intent["version"])
    bot_path, certificate_path = _publication_commit_paths(intent)
    expected_hash = str(intent["candidate_artifact_hash"])
    preexisting_staged = [
        item
        for item in _git("diff", "--cached", "--name-only", check=False).splitlines()
        if item
    ]
    preexisting_scope = classify_status_entries(
        [f"?? {path}" for path in preexisting_staged],
        version,
    )
    blocking = [
        *list(preexisting_scope.get("critical_entries") or []),
        *list(preexisting_scope.get("foreign_bot_entries") or []),
    ]
    if blocking:
        raise RuntimeError(
            "Refusing publication commit with pre-existing blocking staged files: "
            + ", ".join(blocking[:10])
        )
    _git("add", "--", bot_path, certificate_path, check=False)
    ref_updated = False
    index_path = RESULTS_DIR / (
        f".publication-index.{os.getpid()}.{uuid.uuid4().hex}"
    )
    index_lock_path = Path(str(index_path) + ".lock")
    try:
        if hash_path(get_bot_dir(version)) != expected_hash:
            raise RuntimeError("candidate changed while staging publication intent")
        staged = validate_staged_artifact(
            get_bot_dir(version),
            repo_root=PROJECT_ROOT,
        )
        if (
            staged.get("valid") is not True
            or staged.get("working_hash") != expected_hash
            or staged.get("staged_hash") != expected_hash
        ):
            raise RuntimeError(
                "staged Git blobs do not reproduce frozen publication candidate"
            )
        staged_files = [
            item
            for item in _git(
                "diff", "--cached", "--name-only", check=False
            ).splitlines()
            if item
        ]
        bot_prefix = bot_path.rstrip("/") + "/"
        scoped = [
            item
            for item in staged_files
            if item == certificate_path or item.startswith(bot_prefix)
        ]
        outside = [item for item in staged_files if item not in scoped]
        outside_scope = classify_status_entries(
            [f"?? {path}" for path in outside], version
        )
        unexpected = [
            *list(outside_scope.get("critical_entries") or []),
            *list(outside_scope.get("foreign_bot_entries") or []),
        ]
        if not any(item.startswith(bot_prefix) for item in scoped):
            raise RuntimeError("publication commit has no staged candidate files")
        if certificate_path not in scoped:
            raise RuntimeError("publication commit has no staged certificate")
        if unexpected:
            raise RuntimeError(
                "publication commit observed unexpected staged files: "
                + ", ".join(unexpected[:10])
            )
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        parent_oid = _git("rev-parse", "refs/heads/main").strip()
        _git_with_index(index_path, "read-tree", parent_oid)
        _git_with_index(
            index_path,
            "add",
            "-A",
            "--",
            bot_path,
            certificate_path,
        )
        tree_oid = _git_with_index(index_path, "write-tree")
        if not re.fullmatch(r"[0-9a-f]{40}", tree_oid):
            raise RuntimeError("private publication index did not produce a tree")
        _validate_frozen_publication_tree(
            intent,
            tree_oid=tree_oid,
            parent_oid=parent_oid,
        )
        commit_oid = _publication_commit_object(
            tree_oid,
            parent_oid,
            str(intent["commit_message"]),
        )
        # The tree and parent are now fixed objects. update-ref is the only
        # branch mutation and refuses a concurrently moved main ref.
        _git(
            "update-ref",
            "refs/heads/main",
            commit_oid,
            parent_oid,
        )
        ref_updated = True
    except Exception:
        if not ref_updated:
            _git(
                "restore",
                "--staged",
                "--",
                bot_path,
                certificate_path,
                check=False,
            )
        raise
    finally:
        index_path.unlink(missing_ok=True)
        index_lock_path.unlink(missing_ok=True)
    publish_runtime_expected_head("bot_publication_commit", version=version)
    _validate_existing_publication_commit(intent, commit_oid)
    return commit_oid


def _validate_local_publication_refs(intent: dict, commit_oid: str) -> dict:
    tag = str(intent["completion_tag"])
    high_water = str(intent["high_water_tag"])
    issues = []
    refs: dict[str, dict[str, str]] = {}
    for name in (tag, high_water):
        ref = f"refs/tags/{name}"
        tag_type = _git("cat-file", "-t", ref, check=False).strip()
        object_oid = _git("rev-parse", ref, check=False).strip()
        peeled_oid = _git("rev-parse", f"{ref}^{{commit}}", check=False).strip()
        refs[name] = {
            "type": tag_type,
            "object_oid": object_oid,
            "peeled_commit_oid": peeled_oid,
        }
        if tag_type != "tag":
            issues.append(f"local_ref_not_annotated:{name}")
        if peeled_oid != commit_oid:
            issues.append(f"local_ref_commit_mismatch:{name}")
    contents = _git(
        "for-each-ref",
        "--format=%(contents)",
        f"refs/tags/{tag}",
        check=False,
    ).strip()
    if contents != str(intent.get("tag_message") or "").strip():
        issues.append("completion_tag_message_mismatch")
    if issues:
        raise RuntimeError("invalid local publication refs: " + "; ".join(issues))
    return refs


def remote_completion_ref_snapshot() -> dict[str, str]:
    """Return the exact remote active-epoch completion-tag namespace."""

    raw = _git("ls-remote", "origin", "refs/tags/national-bot-v*")
    refs: dict[str, str] = {}
    for line in raw.splitlines():
        oid, separator, ref = line.partition("\t")
        if (
            separator
            and len(oid) == 40
            and ref.startswith("refs/tags/national-bot-v")
        ):
            refs[ref] = oid
    return dict(sorted(refs.items()))


@contextmanager
def _publication_checkpoint_linearization_lock():
    """Fence publishing checkpoint writers across authority-check + push."""

    with _locked_state_sidecar(
        PIPELINE_STATE_FILE,
        lock_type=fcntl.LOCK_EX,
    ):
        yield


def _push_first_strict_publication(
    intent: dict,
    commit_oid: str,
    local_refs: dict,
    *,
    pre_push_authority,
) -> bool:
    """CAS the first strict publication without any reconcile/merge window.

    The first strict bot changes the authority regime from the migration seed
    to the normal strict pool.  Its frozen ``origin/main`` is therefore a
    compare-and-swap precondition, not a merge base.  The completion and
    high-water tags use ordinary create-only refspecs; no force option ever
    applies to a tag.  ``--atomic`` makes a concurrent main/tag change reject
    the complete ref set with no partial remote effects.
    """

    if list(intent.get("prepublication_strict_bots") or []):
        raise RuntimeError("first-strict publication CAS used for a non-first bot")
    if not callable(pre_push_authority):
        raise RuntimeError("first-strict publication requires a pre-push authority check")
    baseline = str(intent.get("baseline_remote_main") or "")
    completion = str(intent.get("completion_tag") or "")
    high_water = str(intent.get("high_water_tag") or "")
    local_main = _git("rev-parse", "refs/heads/main", check=False).strip()
    if not (
        len(baseline) == 40
        and len(local_main) == 40
        and _git_command_succeeds("merge-base", "--is-ancestor", baseline, local_main)
        and _git_command_succeeds("merge-base", "--is-ancestor", commit_oid, local_main)
    ):
        raise RuntimeError(
            "first-strict publication local main is not a fast-forward of the frozen remote baseline"
        )

    # Fetch is read-only with respect to the remote.  It makes a strict bot
    # published since intent creation visible to the authority callback, while
    # the later main lease closes the fetch/check/push race.
    _git("fetch", "origin", "--prune", "--tags")
    wanted = (
        "refs/heads/main",
        "refs/tags/national-bot-v*",
        f"refs/tags/{high_water}",
    )
    raw = _git("ls-remote", "origin", *wanted)
    remote_refs: dict[str, str] = {}
    for line in raw.splitlines():
        oid, separator, ref = line.partition("\t")
        if separator and oid and ref:
            remote_refs[ref] = oid
    if remote_refs.get("refs/heads/main") != baseline:
        raise RuntimeError(
            "first-strict publication blocked: origin/main changed after intent baseline"
        )
    remote_completion_refs = dict(sorted(
        (ref, oid)
        for ref, oid in remote_refs.items()
        if ref.startswith("refs/tags/national-bot-v")
    ))
    if remote_completion_refs != dict(
        intent.get("baseline_remote_completion_refs") or {}
    ):
        raise RuntimeError(
            "first-strict publication blocked: remote strict completion refs "
            "changed after intent baseline"
        )
    occupied = [
        ref for ref in (
            f"refs/tags/{completion}",
            f"refs/tags/{high_water}",
        )
        if remote_refs.get(ref)
    ]
    if occupied:
        raise RuntimeError(
            "first-strict publication blocked: create-only remote tag already exists: "
            + ", ".join(occupied)
        )

    with _publication_checkpoint_linearization_lock():
        # The callback re-reads the checkpoint while this stable sidecar lock
        # excludes every normal checkpoint writer.  Keep the lock until the
        # atomic remote ref transaction has linearized.
        pre_push_authority()

        # A callback must not be able to move any frozen local source ref.
        # Remote races are handled by the leases below.
        if _git("rev-parse", "refs/heads/main", check=False).strip() != local_main:
            raise RuntimeError("first-strict publication local main changed during authority check")
        for name in (completion, high_water):
            expected = str((local_refs.get(name) or {}).get("object_oid") or "")
            current = _git("rev-parse", f"refs/tags/{name}", check=False).strip()
            if not expected or current != expected:
                raise RuntimeError(
                    f"first-strict publication local tag changed during authority check: {name}"
                )

        refspecs = (
            "refs/heads/main:refs/heads/main",
            f"refs/tags/{completion}:refs/tags/{completion}",
            f"refs/tags/{high_water}:refs/tags/{high_water}",
        )
        try:
            _git(
                "push",
                "--atomic",
                f"--force-with-lease=refs/heads/main:{baseline}",
                "origin",
                *refspecs,
            )
        except Exception as exc:
            raise RuntimeError(
                "first-strict publication atomic lease failed; no publication refs were accepted: "
                f"{type(exc).__name__}: {str(exc)[:300]}"
            ) from exc
    return True


def ensure_bot_git_publication(
    publication_intent: dict,
    *,
    official_certificate: dict,
    pre_push_authority=None,
) -> dict:
    """Idempotently converge intent_recorded -> committed -> local refs -> push.

    Existing completion tags are immutable: this function never deletes,
    recreates, or force-updates one.  Recovery reconstructs progress from Git
    rather than trusting a caller-supplied phase.
    """

    from bot_artifact import hash_path, validate_completion_tag
    from official_certification import FULL_POLICY_ID
    from publication_transaction import publication_intent_structure_errors

    intent = dict(publication_intent or {})
    errors = publication_intent_structure_errors(intent)
    if errors:
        raise RuntimeError("invalid publication intent: " + "; ".join(errors))
    version = int(intent["version"])
    certificate = dict(official_certificate or {})
    expected_certificate = {
        "certificate_digest": intent.get("official_certificate_digest"),
        "candidate_hash": intent.get("candidate_artifact_hash"),
        "policy_id": intent.get("official_policy_id"),
    }
    for field, expected in expected_certificate.items():
        if certificate.get(field) != expected:
            raise RuntimeError(f"official certificate {field} differs from publication intent")
    if certificate.get("policy_id") != FULL_POLICY_ID:
        raise RuntimeError("publication intent is not bound to the full official policy")
    if hash_path(get_bot_dir(version)) != intent.get("candidate_artifact_hash"):
        raise RuntimeError("candidate changed after publication intent was recorded")
    _validate_publication_certificate_file(intent)
    _require_national_epoch_registry_for_commit()
    _git_ensure_main_branch()

    with bot_publication_lock():
        commit_oid = _resolve_existing_publication_commit(intent)
        if not commit_oid:
            commit_oid = _create_publication_commit(intent)

        _advance_national_epoch_high_water(version)
        tag = str(intent["completion_tag"])
        if _git("tag", "-l", tag, check=False).strip():
            # Create-only semantics: an existing tag is evidence to validate,
            # never mutable state to repair in place.
            existing_target = _git(
                "rev-parse", f"refs/tags/{tag}^{{commit}}", check=False
            ).strip()
            if existing_target != commit_oid:
                raise RuntimeError("existing completion tag points at a different commit")
        else:
            _git(
                "tag",
                "-a",
                tag,
                commit_oid,
                "-m",
                str(intent["tag_message"]),
            )

        local_refs = _validate_local_publication_refs(intent, commit_oid)
        tag_validation = validate_completion_tag(
            get_bot_dir(version),
            expected_metadata={
                "official-certificate": str(
                    intent["official_certificate_digest"]
                ),
                "official-candidate-hash": str(
                    intent["candidate_artifact_hash"]
                ),
                "official-policy": str(intent["official_policy_id"]),
            },
            certificate_path=str(intent["certificate_relative_path"]),
        )
        if tag_validation.get("valid") is not True:
            raise RuntimeError(
                "completion tag failed frozen publication validation: "
                + ", ".join(tag_validation.get("issues") or [])
            )
        push_attempted = bool(
            intent.get("remote_publication_enabled")
            or intent.get("remote_publication_required")
        )
        push_ok = False
        already_remote = False
        if push_attempted:
            provisional_state = {
                "publication_id": intent["publication_id"],
                "version": version,
                "commit_oid": commit_oid,
                "local_refs": local_refs,
            }
            existing_remote = verify_remote_bot_publication(
                intent,
                local_state=provisional_state,
            )
            already_remote = existing_remote.get("valid") is True
            if already_remote:
                push_ok = True
            elif not list(intent.get("prepublication_strict_bots") or []):
                push_ok = _push_first_strict_publication(
                    intent,
                    commit_oid,
                    local_refs,
                    pre_push_authority=pre_push_authority,
                )
            else:
                if not callable(pre_push_authority):
                    raise RuntimeError(
                        "publication requires a pre-push authority check"
                    )
                with _publication_checkpoint_linearization_lock():
                    pre_push_authority()
                    push_ok = git_push_refs(
                        "main",
                        tag,
                        str(intent["high_water_tag"]),
                    )
            publish_runtime_expected_head(
                "bot_publication_push", version=version
            )
            _clear_remote_publication_cache()
        return {
            "publication_id": intent["publication_id"],
            "version": version,
            "commit_oid": commit_oid,
            "local_refs": local_refs,
            "local_valid": True,
            "push_attempted": push_attempted,
            "push_ok": bool(push_ok),
            "already_remote": already_remote,
        }


def verify_remote_bot_publication(
    publication_intent: dict,
    *,
    local_state: dict | None = None,
) -> dict:
    """Independently prove remote tag objects, peeled commits, and main reachability."""

    intent = dict(publication_intent or {})
    version = int(intent.get("version") or -1)
    commit_oid = str((local_state or {}).get("commit_oid") or "")
    if not commit_oid:
        try:
            commit_oid = _git(
                "rev-parse",
                f"refs/tags/{intent['completion_tag']}^{{commit}}",
                check=False,
            ).strip()
        except Exception:
            commit_oid = ""
    tag_names = [
        str(intent.get("completion_tag") or ""),
        str(intent.get("high_water_tag") or ""),
    ]
    wanted = ["refs/heads/main"]
    for name in tag_names:
        wanted.extend((f"refs/tags/{name}", f"refs/tags/{name}^{{}}"))
    try:
        raw = _git("ls-remote", "origin", *wanted)
    except Exception as exc:
        return {
            "valid": False,
            "version": version,
            "issues": [f"remote_refs_unavailable:{type(exc).__name__}"],
        }
    remote_refs: dict[str, str] = {}
    for line in raw.splitlines():
        oid, separator, ref = line.partition("\t")
        if separator and oid and ref:
            remote_refs[ref] = oid
    issues: list[str] = []
    remote_main = remote_refs.get("refs/heads/main", "")
    if len(remote_main) != 40:
        issues.append("remote_main_missing")
    local_refs = (local_state or {}).get("local_refs") or {}
    for name in tag_names:
        local_object = str(
            (local_refs.get(name) or {}).get("object_oid")
            or _git("rev-parse", f"refs/tags/{name}", check=False).strip()
        )
        local_peeled = str(
            (local_refs.get(name) or {}).get("peeled_commit_oid")
            or _git(
                "rev-parse", f"refs/tags/{name}^{{commit}}", check=False
            ).strip()
        )
        if remote_refs.get(f"refs/tags/{name}") != local_object:
            issues.append(f"remote_tag_object_mismatch:{name}")
        if remote_refs.get(f"refs/tags/{name}^{{}}") != local_peeled:
            issues.append(f"remote_tag_peeled_mismatch:{name}")
        if local_peeled != commit_oid:
            issues.append(f"local_tag_commit_mismatch:{name}")
    if not issues:
        try:
            _git(
                "fetch",
                "--no-tags",
                "origin",
                "refs/heads/main:refs/remotes/origin/main",
            )
        except Exception as exc:
            issues.append(f"remote_main_fetch_failed:{type(exc).__name__}")
        else:
            fetched = _git("rev-parse", "refs/remotes/origin/main", check=False).strip()
            if fetched != remote_main:
                issues.append("remote_main_fetch_identity_mismatch")
            elif not _git_command_succeeds(
                "merge-base", "--is-ancestor", commit_oid, remote_main
            ):
                issues.append("publication_commit_not_on_remote_main")
    return {
        "valid": not issues,
        "version": version,
        "publication_id": intent.get("publication_id"),
        "commit_oid": commit_oid,
        "remote_main_oid": remote_main,
        "remote_refs": remote_refs,
        "issues": list(dict.fromkeys(issues)),
    }


def git_get_parent(version):
    """从 tag/commit message 解析 parent。"""
    tag = bot_tag(version)
    tags = _git("tag", "-l", tag, check=False)
    if tags:
        commit_hash = _git("rev-list", "-n", "1", tag, check=False).strip()
        if not commit_hash:
            return None
        msg = _git("show", "-s", "--format=%B", commit_hash, check=False)
    else:
        log = _git("log", "--diff-filter=A", "--oneline", "-1", "--",
                    bot_relpath(version) + "/", check=False)
        if not log:
            return None
        commit_hash = log.split()[0]
        msg = _git("show", "-s", "--format=%B", commit_hash, check=False)
    for line in (msg or "").split("\n"):
        if line.strip().startswith("parent:"):
            parent = line.split(":", 1)[1].strip()
            parsed = parse_bot_version(parent)
            return parsed if parsed is not None else parent
    return None


# ──────────────────────────────────────────────
# Generation Archiving
# ──────────────────────────────────────────────

def archive_generation(version, source_v, ckpt):
    """Create a structured archive snapshot for a completed generation.

    Writes results/archive/v{N}.json with key metrics from the pipeline state.
    """
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    snapshot = {
        "version": version,
        "source_v": source_v,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "git_tag": bot_tag(version),
        "evaluation_epoch": EVALUATION_EPOCH,
        "bot_name": bot_name(version),
    }

    audit_context = (ckpt or {}).get("audit_context") or {}
    selection = audit_context.get("selection") or {}
    if (
        int(version) == FIRST_STRICT_POLICY_VERSION
        and isinstance(audit_context.get("protocol_bootstrap"), dict)
    ):
        snapshot["strength_evidence_identity"] = {
            "schema_version": 1,
            "mode": "empty_first_strict_bootstrap",
            "strength_evidence_admitted": False,
            "reason": "strict_policy_pool_empty",
        }
    else:
        evidence = selection.get("evaluation_evidence") or {}
        cutoffs = evidence.get("cutoffs") or {}
        snapshot["strength_evidence_identity"] = {
            "schema_version": 1,
            "mode": "frozen_native_evaluation",
            "strength_evidence_admitted": True,
            "generation_snapshot_manifest_digest": str(
                cutoffs.get("generation_snapshot_manifest_digest") or ""
            ),
            "cycle_manifest_digest": str(cutoffs.get("cycle_manifest_digest") or ""),
            "h2h_snapshot_manifest_digest": str(
                selection.get("h2h_snapshot_manifest_digest") or ""
            ),
            "h2h_snapshot_sha256": str(selection.get("h2h_snapshot_sha256") or ""),
            "selection_view_digest": str(evidence.get("selection_view_digest") or ""),
        }

    try:
        snapshot["git_commit"] = _git("rev-parse", "--short", bot_tag(version), check=False)
    except Exception:
        pass

    ratings = load_ratings()
    p = ratings.get(bot_name(version))
    if p:
        snapshot["rating"] = {"r": round(p.r, 1), "rd": round(p.rd, 1)}

    try:
        from tool_helpers import compute_h2h_avg_winrate, _load_h2h_data
        h2h_wr = compute_h2h_avg_winrate(bot_name(version), _load_h2h_data())
        snapshot["h2h_avg_wr"] = round(h2h_wr, 4)
    except Exception:
        pass

    if ckpt:
        gate_results = ckpt.get("gate_results", {})
        if gate_results.get("review"):
            review_data = gate_results["review"]
            snapshot["review_score"] = review_data.get("quality_score", 0)
            if review_data.get("change_summary"):
                snapshot["reviewer_change_summary"] = review_data["change_summary"]
            if review_data.get("risk_areas"):
                snapshot["reviewer_risk_areas"] = review_data["risk_areas"]
        if gate_results.get("critic"):
            critic_data = gate_results["critic"]
            snapshot["critic_score"] = critic_data.get("score", 0)
            if critic_data.get("strategic_assessment"):
                snapshot["critic_data"] = critic_data
        precommit = gate_results.get("precommit_eval", {})
        if precommit:
            snapshot["precommit_eval"] = {"passed": precommit.get("passed", False)}

    try:
        diff_stat = _git("diff", "--stat", f"{bot_tag(source_v)}..{bot_tag(version)}",
                         "--", bot_relpath(version) + "/", check=False)
        if diff_stat:
            last_line = diff_stat.strip().split("\n")[-1]
            snapshot["diff_stats_raw"] = last_line.strip()
    except Exception:
        pass

    snapshot["pool_size"] = len(get_active_bots())

    # commit_bot clears the active checkpoint before the advisory Archivist
    # runs. Issue one content-bound, single-use handoff so a weak controller
    # cannot replay run_archivist against an arbitrary historical bot/source.
    try:
        from bot_artifact import canonical_digest, hash_path

        receipt_payload = {
            "schema_version": "post-commit-archivist-v1",
            "version": int(version),
            "source_v": int(source_v),
            "bot_tag": bot_tag(version),
            "git_commit": _git(
                "rev-parse",
                bot_tag(version),
                check=False,
            ).strip(),
            "artifact_hash": hash_path(get_bot_dir(version)),
            "issued_at": time.time(),
        }
        snapshot["post_commit_archivist_receipt"] = {
            **receipt_payload,
            "receipt_digest": canonical_digest(receipt_payload),
            "status": "pending",
        }
    except Exception as exc:
        log.error(
            "Could not issue post-commit Archivist receipt for v%s: %s",
            version,
            exc,
        )

    archive_path = ARCHIVE_DIR / f"v{version}.json"
    with open(archive_path, "w") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    return snapshot


def _post_commit_archivist_receipt_validation(
    snapshot,
    version,
    source_v,
    *,
    require_pending=True,
):
    from bot_artifact import canonical_digest, hash_path

    if not isinstance(snapshot, dict):
        return False, "archive_snapshot_missing", None
    receipt = snapshot.get("post_commit_archivist_receipt")
    if not isinstance(receipt, dict):
        return False, "post_commit_archivist_receipt_missing", None
    if receipt.get("schema_version") != "post-commit-archivist-v1":
        return False, "post_commit_archivist_receipt_schema", receipt
    try:
        if int(receipt.get("version")) != int(version):
            return False, "post_commit_archivist_version_mismatch", receipt
        if int(receipt.get("source_v")) != int(source_v):
            return False, "post_commit_archivist_source_mismatch", receipt
    except (TypeError, ValueError):
        return False, "post_commit_archivist_identity_invalid", receipt
    payload = {
        key: receipt.get(key)
        for key in (
            "schema_version",
            "version",
            "source_v",
            "bot_tag",
            "git_commit",
            "artifact_hash",
            "issued_at",
        )
    }
    if receipt.get("receipt_digest") != canonical_digest(payload):
        return False, "post_commit_archivist_digest_mismatch", receipt
    if require_pending and receipt.get("status") != "pending":
        return False, "post_commit_archivist_receipt_consumed", receipt
    if receipt.get("bot_tag") != bot_tag(version) or not git_has_tag(version):
        return False, "post_commit_archivist_tag_mismatch", receipt
    current_commit = _git("rev-parse", bot_tag(version), check=False).strip()
    if not current_commit or receipt.get("git_commit") != current_commit:
        return False, "post_commit_archivist_commit_mismatch", receipt
    try:
        if receipt.get("artifact_hash") != hash_path(get_bot_dir(version)):
            return False, "post_commit_archivist_artifact_mismatch", receipt
    except Exception as exc:
        return False, f"post_commit_archivist_artifact_error:{type(exc).__name__}", receipt
    return True, "", receipt


def validate_post_commit_archivist_receipt(version, source_v):
    """Read-only validation for the no-checkpoint runtime guard."""
    try:
        from post_publication_handoff import pending_handoff_route

        route = pending_handoff_route()
        if route.get("status") != "pending":
            return False, ";".join(
                route.get("issues") or ["post_publication_handoff_missing"]
            ), None
        if (
            int(route.get("version")) != int(version)
            or int(route.get("source_v")) != int(source_v)
        ):
            return False, "post_publication_handoff_subject_mismatch", None
        return True, "", {
            "receipt_digest": route.get("identity_digest"),
            "publication_id": route.get("publication_id"),
            "status": route.get("state"),
            "version": int(version),
            "source_v": int(source_v),
        }
    except Exception as exc:
        return False, f"post_publication_handoff_error:{type(exc).__name__}", None


def consume_post_commit_archivist_receipt(version, source_v):
    """Retired consume-before-work API; callers must use the step journal."""
    return False, "post_commit_archivist_consume_api_retired", None


_ROTATION_PLAN_KIND = "append-log-nondestructive-rotation-v2"
_ROTATION_WATERMARK_KIND = "append-log-archive-watermark-v2"
_ROTATION_SET_PLAN_KIND = "append-log-nondestructive-rotation-plan"
_ROTATION_PLAN_KEYS = frozenset({
    "schema_version", "kind", "version", "source", "start_offset",
    "end_offset", "archive", "archive_sha256", "new_prefix_sha256",
    "previous_watermark_digest", "rotation_id", "state", "digest",
})
_ROTATION_WATERMARK_KEYS = frozenset({
    "schema_version", "kind", "source", "end_offset", "prefix_sha256",
    "last_version", "last_rotation_id", "last_plan_digest",
    "previous_watermark_digest", "digest",
})
_ROTATION_RECEIPT_KEYS = frozenset({
    "source", "rotation_id", "plan_digest", "archive_sha256",
    "start_offset", "end_offset", "source_preserved_append_only",
})
_ROTATION_SET_PLAN_KEYS = frozenset({
    "schema_version", "kind", "version", "publication_id",
    "source_policy", "source_bytes_must_be_preserved", "source_snapshots",
    "expected_rotations", "source_snapshot_set_digest",
    "expected_rotation_set_digest", "authority_digest",
})
_ROTATION_SOURCE_SNAPSHOT_KEYS = frozenset({
    "source", "keep_lines", "snapshot_exists", "snapshot_size",
    "snapshot_sha256", "cold_end_offset", "watermark_end_offset",
    "watermark_digest", "expected_rotation",
})
_ROTATION_SUBJECT_KEYS = frozenset({
    "version", "source", "archive", "start_offset", "end_offset",
    "archive_sha256", "new_prefix_sha256", "previous_watermark_digest",
    "rotation_id", "completed_plan_digest",
})


def _rotation_rules():
    return (
        (Path(WORKER_FAILURES_FILE), 200),
        (Path(MATCH_HISTORY_FILE), 500),
        (Path(RATING_HISTORY_FILE), 100),
        (Path(RESULTS_DIR) / "events.jsonl", 1000),
        (Path(LLM_COSTS_FILE), 200),
    )


def _rotation_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _rotation_paths(source_path: Path, version: int):
    root = Path(ARCHIVE_DIR)
    return (
        root / f"{source_path.stem}_v{int(version)}.jsonl",
        root / f"{source_path.stem}_v{int(version)}.rotation.json",
        root / f"{source_path.stem}.rotation-watermark.json",
    )


def _rotation_set_plan_path(version: int):
    return Path(ARCHIVE_DIR) / f"rotation-set-v{int(version)}.plan.json"


def _rotation_record(path: Path, *, kind: str, keys: frozenset[str]):
    from bot_artifact import canonical_digest

    with _locked_state_sidecar(path, lock_type=fcntl.LOCK_SH):
        existed = os.path.lexists(path)
        raw = _read_regular_state_text(path, allow_missing=True)
    if not existed:
        return None
    if not raw.strip():
        raise RuntimeError(f"archive record empty: {path.name}")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"archive record invalid JSON: {path.name}") from exc
    if not isinstance(payload, dict) or set(payload) != set(keys):
        raise RuntimeError(f"archive record fields mismatch: {path.name}")
    if payload.get("schema_version") != 2 or payload.get("kind") != kind:
        raise RuntimeError(f"archive record schema mismatch: {path.name}")
    unsigned = {key: value for key, value in payload.items() if key != "digest"}
    if payload.get("digest") != canonical_digest(unsigned):
        raise RuntimeError(f"archive record digest mismatch: {path.name}")
    return payload


def _write_rotation_record(
    path: Path,
    payload: dict,
    *,
    kind: str,
    keys: frozenset[str],
):
    from bot_artifact import canonical_digest

    unsigned = dict(payload)
    if set(unsigned) != set(keys) - {"digest"}:
        raise RuntimeError(f"archive record write fields mismatch: {path.name}")
    if unsigned.get("schema_version") != 2 or unsigned.get("kind") != kind:
        raise RuntimeError(f"archive record write schema mismatch: {path.name}")
    final = {**unsigned, "digest": canonical_digest(unsigned)}
    with _locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
        _atomic_publish_state_text(
            path,
            json.dumps(final, indent=2, ensure_ascii=False, sort_keys=True),
        )
    reopened = _rotation_record(path, kind=kind, keys=keys)
    if reopened != final:
        raise RuntimeError(f"archive record publication mismatch: {path.name}")
    return final


def _read_rotation_archive(path: Path):
    if not os.path.lexists(path):
        return None
    with locked_file(path, "rb", lock_type=fcntl.LOCK_SH) as handle:
        return handle.read()


def _publish_rotation_archive(path: Path, raw: bytes):
    path = Path(path)
    with _locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
        existing = None
        if os.path.lexists(path):
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                _assert_open_regular_path(path, handle, label="rotation archive")
                existing = handle.read()
                _assert_open_regular_path(path, handle, label="rotation archive")
        if existing is not None:
            if existing != raw:
                raise RuntimeError(f"archive content mismatch: {path.name}")
            _fsync_regular_state_file_and_parent(path)
            return
        _assert_safe_state_parent(path)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = None
        temporary_identity = None
        try:
            descriptor = os.open(temporary, flags, 0o600)
            offset = 0
            while offset < len(raw):
                written = os.write(descriptor, raw[offset:])
                if written <= 0:
                    raise OSError("rotation archive write made no progress")
                offset += written
            os.fsync(descriptor)
            temporary_identity = os.fstat(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, path)
            live = os.lstat(path)
            if (
                temporary_identity is None
                or not stat.S_ISREG(live.st_mode)
                or live.st_nlink != 1
                or (live.st_dev, live.st_ino)
                != (temporary_identity.st_dev, temporary_identity.st_ino)
                or live.st_size != len(raw)
            ):
                raise OSError("rotation archive publication inode changed")
            _fsync_directory(path.parent)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)


def _base_rotation_watermark(source_path: Path):
    from bot_artifact import canonical_digest

    unsigned = {
        "schema_version": 2,
        "kind": _ROTATION_WATERMARK_KIND,
        "source": source_path.name,
        "end_offset": 0,
        "prefix_sha256": _rotation_digest(b""),
        "last_version": None,
        "last_rotation_id": None,
        "last_plan_digest": None,
        "previous_watermark_digest": None,
    }
    return {**unsigned, "digest": canonical_digest(unsigned)}


def _validate_rotation_plan(
    plan: dict,
    *,
    version: int,
    source_path: Path,
    raw: bytes,
    require_completed: bool,
    require_archive: bool,
):
    from bot_artifact import canonical_digest

    archive_path, _plan_path, _watermark_path = _rotation_paths(
        source_path,
        version,
    )
    if (
        type(plan.get("version")) is not int
        or plan["version"] != int(version)
        or plan.get("source") != source_path.name
        or plan.get("archive") != archive_path.name
        or plan.get("state") not in {"planned", "completed"}
        or (require_completed and plan.get("state") != "completed")
    ):
        raise RuntimeError(f"archive plan identity invalid: {source_path.name}")
    start = plan.get("start_offset")
    end = plan.get("end_offset")
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= len(raw)
    ):
        raise RuntimeError(f"archive plan offsets invalid: {source_path.name}")
    for key in (
        "archive_sha256", "new_prefix_sha256",
        "previous_watermark_digest", "rotation_id", "digest",
    ):
        value = str(plan.get(key) or "")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise RuntimeError(f"archive plan digest invalid:{source_path.name}:{key}")
    archived = raw[start:end]
    subject = {
        "version": int(version),
        "source": source_path.name,
        "start_offset": start,
        "end_offset": end,
        "archive_sha256": _rotation_digest(archived),
        "new_prefix_sha256": _rotation_digest(raw[:end]),
        "previous_watermark_digest": plan["previous_watermark_digest"],
    }
    if (
        plan.get("archive_sha256") != subject["archive_sha256"]
        or plan.get("new_prefix_sha256") != subject["new_prefix_sha256"]
        or plan.get("rotation_id") != canonical_digest(subject)
    ):
        raise RuntimeError(f"archive plan derivation mismatch: {source_path.name}")
    archived_live = _read_rotation_archive(archive_path)
    if archived_live is not None and archived_live != archived:
        raise RuntimeError(f"archive bytes mismatch: {archive_path.name}")
    if require_archive and archived_live is None:
        raise RuntimeError(f"archive bytes missing: {archive_path.name}")
    return archived


def _load_rotation_watermark(source_path: Path, raw: bytes):
    _archive, _plan, watermark_path = _rotation_paths(source_path, 0)
    watermark = _rotation_record(
        watermark_path,
        kind=_ROTATION_WATERMARK_KIND,
        keys=_ROTATION_WATERMARK_KEYS,
    )
    if watermark is None:
        return _base_rotation_watermark(source_path)
    end = watermark.get("end_offset")
    if (
        watermark.get("source") != source_path.name
        or type(end) is not int
        or not 0 < end <= len(raw)
        or watermark.get("prefix_sha256") != _rotation_digest(raw[:end])
        or type(watermark.get("last_version")) is not int
        or int(watermark["last_version"]) < FIRST_STRICT_POLICY_VERSION
    ):
        raise RuntimeError(f"archive watermark identity invalid: {source_path.name}")
    for key in (
        "last_rotation_id", "last_plan_digest", "previous_watermark_digest",
        "digest",
    ):
        value = str(watermark.get(key) or "")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise RuntimeError(f"archive watermark digest invalid:{source_path.name}:{key}")
    prior_version = int(watermark["last_version"])
    _prior_archive, prior_plan_path, _prior_watermark = _rotation_paths(
        source_path,
        prior_version,
    )
    prior_plan = _rotation_record(
        prior_plan_path,
        kind=_ROTATION_PLAN_KIND,
        keys=_ROTATION_PLAN_KEYS,
    )
    if prior_plan is None:
        raise RuntimeError(f"archive watermark plan missing: {source_path.name}")
    _validate_rotation_plan(
        prior_plan,
        version=prior_version,
        source_path=source_path,
        raw=raw,
        require_completed=True,
        require_archive=True,
    )
    if (
        prior_plan["end_offset"] != end
        or prior_plan["new_prefix_sha256"] != watermark["prefix_sha256"]
        or prior_plan["rotation_id"] != watermark["last_rotation_id"]
        or prior_plan["digest"] != watermark["last_plan_digest"]
        or prior_plan["previous_watermark_digest"]
        != watermark["previous_watermark_digest"]
    ):
        raise RuntimeError(f"archive watermark chain mismatch: {source_path.name}")
    return watermark


def _rotation_receipt(plan: dict):
    return {
        "source": plan["source"],
        "rotation_id": plan["rotation_id"],
        "plan_digest": plan["digest"],
        "archive_sha256": plan["archive_sha256"],
        "start_offset": plan["start_offset"],
        "end_offset": plan["end_offset"],
        "source_preserved_append_only": True,
    }


def _rotation_digest_value(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _completed_rotation_plan_digest(subject):
    from bot_artifact import canonical_digest

    unsigned = {
        "schema_version": 2,
        "kind": _ROTATION_PLAN_KIND,
        "version": subject["version"],
        "source": subject["source"],
        "start_offset": subject["start_offset"],
        "end_offset": subject["end_offset"],
        "archive": subject["archive"],
        "archive_sha256": subject["archive_sha256"],
        "new_prefix_sha256": subject["new_prefix_sha256"],
        "previous_watermark_digest": subject["previous_watermark_digest"],
        "rotation_id": subject["rotation_id"],
        "state": "completed",
    }
    return canonical_digest(unsigned)


def _rotation_subject_receipt(subject):
    return {
        "source": subject["source"],
        "rotation_id": subject["rotation_id"],
        "plan_digest": subject["completed_plan_digest"],
        "archive_sha256": subject["archive_sha256"],
        "start_offset": subject["start_offset"],
        "end_offset": subject["end_offset"],
        "source_preserved_append_only": True,
    }


def _validate_archive_rotation_plan_shape(
    rotation_plan,
    *,
    version,
    publication_id=None,
):
    """Validate the self-contained high-level plan without reading effects."""

    from bot_artifact import canonical_digest

    version = int(version)
    if (
        not isinstance(rotation_plan, dict)
        or set(rotation_plan) != set(_ROTATION_SET_PLAN_KEYS)
    ):
        raise RuntimeError("archive rotation set plan fields mismatch")
    observed_publication_id = rotation_plan.get("publication_id")
    if (
        rotation_plan.get("schema_version") != 1
        or rotation_plan.get("kind") != _ROTATION_SET_PLAN_KIND
        or type(rotation_plan.get("version")) is not int
        or rotation_plan["version"] != version
        or not _rotation_digest_value(observed_publication_id)
        or (
            publication_id is not None
            and observed_publication_id != publication_id
        )
        or rotation_plan.get("source_policy") != "append-only-cold-prefix"
        or rotation_plan.get("source_bytes_must_be_preserved") is not True
    ):
        raise RuntimeError("archive rotation set plan identity invalid")
    snapshots = rotation_plan.get("source_snapshots")
    rotations = rotation_plan.get("expected_rotations")
    rules = list(_rotation_rules())
    if not isinstance(snapshots, list) or len(snapshots) != len(rules):
        raise RuntimeError("archive rotation source snapshots incomplete")
    if not isinstance(rotations, list):
        raise RuntimeError("archive rotation expected subjects invalid")

    derived_rotations = []
    seen_sources = set()
    for snapshot, (source_path, keep_lines) in zip(snapshots, rules):
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) != set(_ROTATION_SOURCE_SNAPSHOT_KEYS)
        ):
            raise RuntimeError("archive rotation source snapshot fields mismatch")
        source_name = source_path.name
        if source_name in seen_sources:
            raise RuntimeError("archive rotation rule source duplicated")
        seen_sources.add(source_name)
        size = snapshot.get("snapshot_size")
        cold_end = snapshot.get("cold_end_offset")
        watermark_end = snapshot.get("watermark_end_offset")
        if (
            snapshot.get("source") != source_name
            or snapshot.get("keep_lines") != keep_lines
            or type(snapshot.get("snapshot_exists")) is not bool
            or type(size) is not int
            or type(cold_end) is not int
            or type(watermark_end) is not int
            or not 0 <= watermark_end <= cold_end <= size
            or not _rotation_digest_value(snapshot.get("snapshot_sha256"))
            or not _rotation_digest_value(snapshot.get("watermark_digest"))
        ):
            raise RuntimeError(
                f"archive rotation source snapshot invalid: {source_name}"
            )
        if snapshot["snapshot_exists"] is False:
            base = _base_rotation_watermark(source_path)
            if (
                size != 0
                or cold_end != 0
                or watermark_end != 0
                or snapshot["snapshot_sha256"] != _rotation_digest(b"")
                or snapshot["watermark_digest"] != base["digest"]
            ):
                raise RuntimeError(
                    f"archive rotation absent snapshot invalid: {source_name}"
                )

        expected = snapshot.get("expected_rotation")
        if cold_end <= watermark_end:
            if expected is not None:
                raise RuntimeError(
                    f"archive rotation unexpected subject: {source_name}"
                )
            continue
        if (
            not isinstance(expected, dict)
            or set(expected) != set(_ROTATION_SUBJECT_KEYS)
        ):
            raise RuntimeError(
                f"archive rotation expected subject fields mismatch: {source_name}"
            )
        archive_path, _plan_path, _watermark_path = _rotation_paths(
            source_path,
            version,
        )
        subject = {
            "version": version,
            "source": source_name,
            "start_offset": watermark_end,
            "end_offset": cold_end,
            "archive_sha256": expected.get("archive_sha256"),
            "new_prefix_sha256": expected.get("new_prefix_sha256"),
            "previous_watermark_digest": snapshot["watermark_digest"],
        }
        if (
            expected.get("version") != version
            or expected.get("source") != source_name
            or expected.get("archive") != archive_path.name
            or expected.get("start_offset") != watermark_end
            or expected.get("end_offset") != cold_end
            or not _rotation_digest_value(subject["archive_sha256"])
            or not _rotation_digest_value(subject["new_prefix_sha256"])
            or expected.get("previous_watermark_digest")
            != snapshot["watermark_digest"]
            or expected.get("rotation_id") != canonical_digest(subject)
            or expected.get("completed_plan_digest")
            != _completed_rotation_plan_digest(expected)
        ):
            raise RuntimeError(
                f"archive rotation expected subject invalid: {source_name}"
            )
        derived_rotations.append(expected)

    if rotations != derived_rotations:
        raise RuntimeError("archive rotation expected subject set mismatch")
    if rotation_plan.get("source_snapshot_set_digest") != canonical_digest(
        snapshots
    ):
        raise RuntimeError("archive rotation source snapshot digest mismatch")
    if rotation_plan.get("expected_rotation_set_digest") != canonical_digest(
        rotations
    ):
        raise RuntimeError("archive rotation expected subject digest mismatch")
    unsigned = {
        key: value
        for key, value in rotation_plan.items()
        if key != "authority_digest"
    }
    if rotation_plan.get("authority_digest") != canonical_digest(unsigned):
        raise RuntimeError("archive rotation authority digest mismatch")
    return derived_rotations


def expected_archive_rotation_receipts(
    rotation_plan,
    *,
    version,
    publication_id=None,
):
    """Derive the exact receipt set without relying on low-level plan files."""

    rotations = _validate_archive_rotation_plan_shape(
        rotation_plan,
        version=version,
        publication_id=publication_id,
    )
    return [_rotation_subject_receipt(subject) for subject in rotations]


def _read_archive_rotation_plan_authority(version, *, missing_ok=False):
    path = _rotation_set_plan_path(version)
    if not os.path.lexists(path):
        if missing_ok:
            return None
        raise RuntimeError("archive rotation set plan authority missing")
    with _locked_state_sidecar(path, lock_type=fcntl.LOCK_SH):
        raw = _read_regular_state_text(path, allow_missing=False)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("archive rotation set plan authority invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("archive rotation set plan authority not object")
    return payload


def _publish_archive_rotation_plan_authority(plan):
    """Create one immutable plan authority; never replace existing bytes."""

    version = int(plan["version"])
    path = _rotation_set_plan_path(version)
    _validate_archive_rotation_plan_shape(
        plan,
        version=version,
        publication_id=plan.get("publication_id"),
    )
    Path(ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)
    _fsync_directory(ARCHIVE_DIR)
    encoded = json.dumps(
        plan,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    with _locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
        if os.path.lexists(path):
            existing = _read_regular_state_text(path, allow_missing=False)
            if existing != encoded:
                raise RuntimeError(
                    "archive rotation set plan authority already differs"
                )
            _fsync_regular_state_file_and_parent(path)
            return plan
        # The stable sidecar makes this a create-once publication for every
        # cooperating producer.  Atomic replace of the private inode has no
        # link/unlink crash window, unlike a create-only hardlink sequence.
        _atomic_publish_state_text(path, encoded)
    reopened = _read_archive_rotation_plan_authority(version)
    if reopened != plan:
        raise RuntimeError("archive rotation set plan authority reproof mismatch")
    return plan


def build_archive_rotation_plan(version, publication_id):
    """Freeze every managed source before any archive effect is allowed."""

    from bot_artifact import canonical_digest

    version = int(version)
    if version < FIRST_STRICT_POLICY_VERSION:
        raise RuntimeError("pre_epoch_archive_rotation_forbidden")
    if not _rotation_digest_value(publication_id):
        raise RuntimeError("archive rotation publication identity invalid")
    existing_authority = _read_archive_rotation_plan_authority(
        version,
        missing_ok=True,
    )
    if existing_authority is not None:
        validate_archive_rotation_plan(
            existing_authority,
            version=version,
            publication_id=publication_id,
        )
        return existing_authority

    snapshots = []
    rotations = []
    for source_path, keep_lines in _rotation_rules():
        current_archive, current_plan, watermark_path = _rotation_paths(
            source_path,
            version,
        )
        if os.path.lexists(current_archive) or os.path.lexists(current_plan):
            raise RuntimeError(
                f"archive rotation effect precedes high-level plan: {source_path.name}"
            )
        snapshot_exists = os.path.lexists(source_path)
        if snapshot_exists:
            with locked_file(source_path, "rb", lock_type=fcntl.LOCK_SH) as source:
                raw = source.read()
                watermark = _load_rotation_watermark(source_path, raw)
        else:
            raw = b""
            watermark = _base_rotation_watermark(source_path)
            if os.path.lexists(watermark_path):
                raise RuntimeError(
                    f"archive rotation source missing with authority: {source_path.name}"
                )
        lines = raw.splitlines(keepends=True)
        cold_end = (
            sum(len(line) for line in lines[:-keep_lines])
            if len(lines) > keep_lines
            else 0
        )
        start = int(watermark["end_offset"])
        if cold_end < start:
            cold_end = start
        expected = None
        if cold_end > start:
            archive_path, _plan_path, _watermark_path = _rotation_paths(
                source_path,
                version,
            )
            subject = {
                "version": version,
                "source": source_path.name,
                "start_offset": start,
                "end_offset": cold_end,
                "archive_sha256": _rotation_digest(raw[start:cold_end]),
                "new_prefix_sha256": _rotation_digest(raw[:cold_end]),
                "previous_watermark_digest": watermark["digest"],
            }
            expected = {
                **subject,
                "archive": archive_path.name,
                "rotation_id": canonical_digest(subject),
            }
            expected["completed_plan_digest"] = (
                _completed_rotation_plan_digest(expected)
            )
            rotations.append(expected)
        snapshots.append({
            "source": source_path.name,
            "keep_lines": keep_lines,
            "snapshot_exists": snapshot_exists,
            "snapshot_size": len(raw),
            "snapshot_sha256": _rotation_digest(raw),
            "cold_end_offset": cold_end,
            "watermark_end_offset": start,
            "watermark_digest": watermark["digest"],
            "expected_rotation": expected,
        })
    plan = {
        "schema_version": 1,
        "kind": _ROTATION_SET_PLAN_KIND,
        "version": version,
        "publication_id": publication_id,
        "source_policy": "append-only-cold-prefix",
        "source_bytes_must_be_preserved": True,
        "source_snapshots": snapshots,
        "expected_rotations": rotations,
        "source_snapshot_set_digest": canonical_digest(snapshots),
        "expected_rotation_set_digest": canonical_digest(rotations),
    }
    plan["authority_digest"] = canonical_digest(plan)
    _validate_archive_rotation_plan_shape(
        plan,
        version=version,
        publication_id=publication_id,
    )
    return _publish_archive_rotation_plan_authority(plan)


def validate_archive_rotation_plan(
    rotation_plan,
    *,
    version,
    publication_id=None,
):
    """Reprove the frozen source prefixes and current predecessor authority."""

    rotations = _validate_archive_rotation_plan_shape(
        rotation_plan,
        version=version,
        publication_id=publication_id,
    )
    authority = _read_archive_rotation_plan_authority(int(version))
    if authority != rotation_plan:
        raise RuntimeError("archive rotation set plan authority mismatch")
    expected_by_source = {item["source"]: item for item in rotations}
    for snapshot, (source_path, keep_lines) in zip(
        rotation_plan["source_snapshots"],
        _rotation_rules(),
    ):
        if not os.path.lexists(source_path):
            if snapshot["snapshot_exists"]:
                raise RuntimeError(
                    f"archive rotation snapshot source missing: {source_path.name}"
                )
            raw = b""
            current_watermark = _base_rotation_watermark(source_path)
        else:
            with locked_file(source_path, "rb", lock_type=fcntl.LOCK_SH) as source:
                raw = source.read()
                current_watermark = _load_rotation_watermark(source_path, raw)
        snapshot_size = snapshot["snapshot_size"]
        if (
            len(raw) < snapshot_size
            or _rotation_digest(raw[:snapshot_size])
            != snapshot["snapshot_sha256"]
        ):
            raise RuntimeError(
                f"archive rotation source prefix changed: {source_path.name}"
            )
        frozen = raw[:snapshot_size]
        lines = frozen.splitlines(keepends=True)
        cold_end = (
            sum(len(line) for line in lines[:-keep_lines])
            if len(lines) > keep_lines
            else 0
        )
        if cold_end < snapshot["watermark_end_offset"]:
            cold_end = snapshot["watermark_end_offset"]
        if cold_end != snapshot["cold_end_offset"]:
            raise RuntimeError(
                f"archive rotation frozen range changed: {source_path.name}"
            )
        expected = expected_by_source.get(source_path.name)
        _archive_path, low_plan_path, _watermark_path = _rotation_paths(
            source_path,
            int(version),
        )
        low_plan = None
        if os.path.lexists(low_plan_path):
            low_plan = _rotation_record(
                low_plan_path,
                kind=_ROTATION_PLAN_KIND,
                keys=_ROTATION_PLAN_KEYS,
            )
        if expected is None:
            if low_plan is not None:
                raise RuntimeError(
                    f"archive rotation unplanned low-level plan: {source_path.name}"
                )
            if current_watermark["digest"] != snapshot["watermark_digest"]:
                raise RuntimeError(
                    f"archive rotation no-op predecessor changed: {source_path.name}"
                )
            continue
        if low_plan is None:
            if current_watermark["digest"] != snapshot["watermark_digest"]:
                raise RuntimeError(
                    f"archive rotation predecessor changed: {source_path.name}"
                )
            continue
        _validate_rotation_plan(
            low_plan,
            version=int(version),
            source_path=source_path,
            raw=raw,
            require_completed=low_plan.get("state") == "completed",
            require_archive=low_plan.get("state") == "completed",
        )
        for key in _ROTATION_SUBJECT_KEYS - {"completed_plan_digest"}:
            if low_plan.get(key) != expected.get(key):
                raise RuntimeError(
                    f"archive rotation low-level plan mismatch: {source_path.name}"
                )
        if expected["completed_plan_digest"] != _completed_rotation_plan_digest(
            expected
        ):
            raise RuntimeError(
                f"archive rotation completion digest mismatch: {source_path.name}"
            )
        if (
            low_plan.get("state") == "completed"
            and low_plan.get("digest") != expected["completed_plan_digest"]
        ):
            raise RuntimeError(
                f"archive rotation completed plan mismatch: {source_path.name}"
            )
    return rotation_plan


def archive_rotate_files(version, rotation_plan):
    """Copy new cold JSONL ranges without truncating their live authority."""

    version = int(version)
    if version < FIRST_STRICT_POLICY_VERSION:
        raise RuntimeError("pre_epoch_archive_rotation_forbidden")
    validate_archive_rotation_plan(
        rotation_plan,
        version=version,
        publication_id=rotation_plan.get("publication_id")
        if isinstance(rotation_plan, dict)
        else None,
    )
    expected_by_source = {
        item["source"]: item
        for item in rotation_plan["expected_rotations"]
    }
    Path(ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)
    _fsync_directory(ARCHIVE_DIR)
    receipts = []
    for source_path, _keep_lines in _rotation_rules():
        expected = expected_by_source.get(source_path.name)
        if expected is None:
            continue
        if not os.path.lexists(source_path):
            raise RuntimeError(
                f"archive rotation planned source missing: {source_path.name}"
            )
        archive_path, plan_path, watermark_path = _rotation_paths(
            source_path,
            version,
        )
        with locked_file(source_path, "rb", lock_type=fcntl.LOCK_EX) as source:
            raw = source.read()
            watermark = _load_rotation_watermark(source_path, raw)
            plan = _rotation_record(
                plan_path,
                kind=_ROTATION_PLAN_KIND,
                keys=_ROTATION_PLAN_KEYS,
            )
            if plan is None:
                if os.path.lexists(archive_path):
                    raise RuntimeError(f"unclaimed archive exists: {archive_path.name}")
                if watermark["digest"] != expected["previous_watermark_digest"]:
                    raise RuntimeError(
                        f"archive rotation planned predecessor mismatch: {source_path.name}"
                    )
                plan = _write_rotation_record(
                    plan_path,
                    {
                        "schema_version": 2,
                        "kind": _ROTATION_PLAN_KIND,
                        **{
                            key: value
                            for key, value in expected.items()
                            if key != "completed_plan_digest"
                        },
                        "state": "planned",
                    },
                    kind=_ROTATION_PLAN_KIND,
                    keys=_ROTATION_PLAN_KEYS,
                )
            for key in _ROTATION_SUBJECT_KEYS - {"completed_plan_digest"}:
                if plan.get(key) != expected.get(key):
                    raise RuntimeError(
                        f"archive rotation low-level plan mismatch: {source_path.name}"
                    )
            archived = _validate_rotation_plan(
                plan,
                version=version,
                source_path=source_path,
                raw=raw,
                require_completed=False,
                require_archive=False,
            )
            start = int(plan["start_offset"])
            end = int(plan["end_offset"])
            watermark_end = int(watermark["end_offset"])
            if watermark_end < start or start < watermark_end < end:
                raise RuntimeError(f"archive watermark overlaps plan: {source_path.name}")
            if watermark_end == start:
                if plan["previous_watermark_digest"] != watermark["digest"]:
                    raise RuntimeError(f"archive plan predecessor mismatch: {source_path.name}")
                _publish_rotation_archive(archive_path, archived)
                if plan["state"] != "completed":
                    plan = _write_rotation_record(
                        plan_path,
                        {key: value for key, value in plan.items() if key != "digest"} | {"state": "completed"},
                        kind=_ROTATION_PLAN_KIND,
                        keys=_ROTATION_PLAN_KEYS,
                    )
                if plan["digest"] != expected["completed_plan_digest"]:
                    raise RuntimeError(
                        f"archive rotation completed plan mismatch: {source_path.name}"
                    )
                _write_rotation_record(
                    watermark_path,
                    {
                        "schema_version": 2,
                        "kind": _ROTATION_WATERMARK_KIND,
                        "source": source_path.name,
                        "end_offset": end,
                        "prefix_sha256": plan["new_prefix_sha256"],
                        "last_version": version,
                        "last_rotation_id": plan["rotation_id"],
                        "last_plan_digest": plan["digest"],
                        "previous_watermark_digest": plan["previous_watermark_digest"],
                    },
                    kind=_ROTATION_WATERMARK_KIND,
                    keys=_ROTATION_WATERMARK_KEYS,
                )
            elif watermark_end >= end:
                _validate_rotation_plan(
                    plan,
                    version=version,
                    source_path=source_path,
                    raw=raw,
                    require_completed=True,
                    require_archive=True,
                )
                if plan["digest"] != expected["completed_plan_digest"]:
                    raise RuntimeError(
                        f"archive rotation completed plan mismatch: {source_path.name}"
                    )
            receipts.append(_rotation_receipt(plan))
    validate_archive_rotation_receipts(
        version,
        receipts,
        rotation_plan=rotation_plan,
    )
    return receipts


def validate_archive_rotation_receipts(version, receipts, *, rotation_plan):
    """Pure read/reproof of an already planned rotation; creates no files."""

    version = int(version)
    if not isinstance(receipts, list):
        raise RuntimeError("archive rotation receipts must be a list")
    validate_archive_rotation_plan(
        rotation_plan,
        version=version,
        publication_id=rotation_plan.get("publication_id")
        if isinstance(rotation_plan, dict)
        else None,
    )
    expected_receipts = expected_archive_rotation_receipts(
        rotation_plan,
        version=version,
        publication_id=rotation_plan.get("publication_id")
        if isinstance(rotation_plan, dict)
        else None,
    )
    by_name = {path.name: path for path, _keep in _rotation_rules()}
    supplied = {}
    for receipt in receipts:
        if not isinstance(receipt, dict) or set(receipt) != set(_ROTATION_RECEIPT_KEYS):
            raise RuntimeError("archive rotation receipt fields mismatch")
        source_name = receipt.get("source")
        if source_name in supplied or source_name not in by_name:
            raise RuntimeError("archive rotation receipt source invalid")
        supplied[source_name] = receipt

    # The high-level plan is the authority even before the first low-level
    # per-source plan exists.  Reject omissions before inspecting effects so a
    # forged ``rotations=[]`` cannot vacuously certify a cold source.
    if receipts != expected_receipts:
        expected_names = {item["source"] for item in expected_receipts}
        supplied_names = set(supplied)
        missing = sorted(expected_names - supplied_names)
        if missing:
            raise RuntimeError(
                f"archive rotation receipt missing: {missing[0]}"
            )
        unexpected = sorted(supplied_names - expected_names)
        if unexpected:
            raise RuntimeError(
                f"archive rotation receipt unexpected: {unexpected[0]}"
            )
        raise RuntimeError("archive rotation receipt set mismatch")

    verified = []
    expected_by_source = {
        item["source"]: item for item in expected_receipts
    }
    for source_path, _keep_lines in _rotation_rules():
        source_name = source_path.name
        if source_name not in expected_by_source:
            continue
        _archive_path, plan_path, _watermark_path = _rotation_paths(
            source_path,
            version,
        )
        if not os.path.lexists(plan_path):
            raise RuntimeError(f"archive rotation plan missing: {source_name}")
        if not os.path.lexists(source_path):
            raise RuntimeError(
                f"archive rotation source missing: {source_name}"
            )
        with locked_file(source_path, "rb", lock_type=fcntl.LOCK_SH) as source:
            raw = source.read()
            watermark = _load_rotation_watermark(source_path, raw)
            plan = _rotation_record(
                plan_path,
                kind=_ROTATION_PLAN_KIND,
                keys=_ROTATION_PLAN_KEYS,
            )
            if plan is None:
                raise RuntimeError(f"archive rotation plan missing: {source_name}")
            _validate_rotation_plan(
                plan,
                version=version,
                source_path=source_path,
                raw=raw,
                require_completed=True,
                require_archive=True,
            )
            if int(watermark["end_offset"]) < int(plan["end_offset"]):
                raise RuntimeError(f"archive rotation watermark behind: {source_name}")
            expected = _rotation_receipt(plan)
            receipt = supplied.get(source_name)
            if receipt is None:
                raise RuntimeError(
                    f"archive rotation receipt missing: {source_name}"
                )
            if receipt != expected:
                raise RuntimeError(f"archive rotation receipt mismatch: {source_name}")
            verified.append(expected)
    return verified


def archive_old_logs(keep_generations=5):
    """Retired unsafe API; strict handoff cleanup owns explicit log paths."""

    raise RuntimeError(
        "archive_old_logs_retired_use_post_publication_strict_log_cleanup"
    )


# ──────────────────────────────────────────────
# Re-exports from extracted modules
# ──────────────────────────────────────────────

from daemon_management import (  # noqa: F401, E402
    daemon_proc, _daemon_lock, _atexit_registered, _daemon_shutting_down,
    start_daemon, stop_daemon, is_daemon_alive, daemon_monitor_thread,
    _drain_stdout,
)
from llm_query import (  # noqa: F401, E402
    _is_rate_limited, _is_quota_exceeded, _trim_to_budget,
    run_claude_query, parse_json_output,
)
from rate_limiter import rate_limiter, RateLimiter  # noqa: F401, E402
from code_verification import (  # noqa: F401, E402
    verify_code, check_code_size, run_smoke_test,
    run_decision_test_details, run_national_protocol_tests,
    run_import_contract_test,
)
