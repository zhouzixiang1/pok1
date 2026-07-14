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


# Phase 4 feature flag (default OFF). PSRO = Pipeline Policy Response Operator
# MVP: when enabled, a MixtureBot (bots/mixture_main/) meta-opponent is injected
# as a telemetry-only opponent in run_precommit_eval. OFF = byte-identical
# precommit path (no mixture_main opponent), guaranteeing zero regression for
# the engine/2-player contract. Toggle via env var POK_PSRO_ENABLED=1.
PSRO_ENABLED = os.environ.get("POK_PSRO_ENABLED", "0") == "1"
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
LLM_COSTS_FILE = RESULTS_DIR / "llm_costs.jsonl"
RATING_HISTORY_FILE = RESULTS_DIR / "rating_history.jsonl"
ABANDONED_VERSIONS_FILE = RESULTS_DIR / "abandoned_versions.jsonl"
REAPED_BOTS_FILE = RESULTS_DIR / "reaped_bots.jsonl"

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
    """Serialize same-process threads by path and processes with ``flock``."""
    with _thread_lock_for(path):
        with _locked_file_os(
            path,
            mode=mode,
            lock_type=lock_type,
            encoding=encoding,
        ) as handle:
            yield handle


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


def read_locked_json(path, default=None):
    """Read a JSON file with shared lock. Returns default on any error."""
    try:
        with locked_file(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return default


def write_locked_json(path, data, indent=2):
    """Write a JSON file atomically: write to tmp, fsync, replace under exclusive lock."""
    path = Path(path)
    os.makedirs(str(path.parent), exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Lock the target path without truncating it. Using mode="w" here would
    # empty the live JSON before the tmp+replace step; if the daemon is killed
    # between truncate and replace, readers see a permanent 0-byte file.
    with locked_file(path, "a+", encoding="utf-8", lock_type=fcntl.LOCK_EX) as _lock_guard:
        # Write to temp file, then atomically replace
        with open(str(tmp), "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=indent, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))


def append_locked_jsonl(path, entry):
    """Append a JSON entry as a single line to a JSONL file with exclusive lock."""
    with locked_file(path, "a", encoding="utf-8", lock_type=fcntl.LOCK_EX) as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


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


def write_pipeline_checkpoint(next_v, source_v, stage, master_plan=None,
                               reviewer_feedback="", generation_attempt=0,
                               gate_results=None, worker_failure_count=None,
                               worker_invocation_count=None,
                               parent2_v=None, direction_audit=None,
                               audit_context=None, reset_generation_attempt=False,
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
                               reset_runtime_contract_ledger=False,
                               expected_runtime_contract_ledger_digest=None,
                               runtime_contract_ledger_reset_reason=None,
                               publication_intent=None,
                               expected_checkpoint_revision=None,
                               expected_checkpoint_stage=None,
                               expected_workflow_run_id=None,
                               workflow_run_id=None):
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

    # Single exclusive lock covers read-merge-write-rename to prevent TOCTOU
    checkpoint_lock = PIPELINE_STATE_FILE.with_suffix(
        PIPELINE_STATE_FILE.suffix + ".lock"
    )
    # Lock a stable sidecar inode. Locking PIPELINE_STATE_FILE itself is unsafe
    # because os.replace swaps that inode while waiters may still hold an open
    # descriptor to the retired file and later overwrite a newer projection.
    with locked_file(checkpoint_lock, "a+", lock_type=fcntl.LOCK_EX):
        try:
            raw = PIPELINE_STATE_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw = ""
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
                "timed_out",
                "archived",
                "abandoned",
            }:
                from checkpoint_schema import checkpoint_epoch_errors

                epoch_errors = checkpoint_epoch_errors(existing)
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
                "timed_out",
                "infra_timed_out",
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
            dead_stages = {None, "timed_out", "archived", "abandoned"}
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

        # Explicit reset: run_master produced a fresh plan → clear critic-rejection counter.
        # Without this, generation_attempt stays >=1 after a critic rejection and
        # execute_workers' circuit breaker (tool_planning.py:558) loops forever demanding
        # a new plan that run_master already produced.
        if reset_generation_attempt:
            existing_generation_attempt = 0
        if reset_audit_attempt:
            existing_audit_attempt = 0
        if reset_precommit_attempt:
            existing_precommit_attempt = 0
        if timeout_extensions is not None:
            existing_timeout_extensions = int(timeout_extensions)

        if gate_results:
            existing_gate_results.update(gate_results)
        existing_gate_results = _prune_gate_results_for_stage(stage, existing_gate_results)
        if worker_failure_count is not None:
            existing_failure_count = worker_failure_count
        elif worker_invocation_count is not None:
            existing_failure_count = worker_invocation_count
        if direction_audit is not None:
            existing_direction_audit = direction_audit
        if audit_context is not None:
            existing_audit_context.update(audit_context)
        if existing_epoch_binding is None:
            try:
                from checkpoint_schema import build_checkpoint_epoch_binding

                existing_epoch_binding = build_checkpoint_epoch_binding(
                    next_v=next_v,
                    source_v=source_v,
                    parent2_v=existing_parent2_v,
                    audit_context=existing_audit_context,
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
        if repair_baseline_artifact_hash is not None:
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

        epoch_errors = checkpoint_epoch_errors(state)
        if epoch_errors:
            log.error(
                "Refusing checkpoint whose strict epoch binding does not match "
                "the final CAS projection: %s",
                epoch_errors,
            )
            return False

        # Atomic write: tmp + fsync + rename, all under the same lock
        tmp = PIPELINE_STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            f.write(json.dumps(state, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(PIPELINE_STATE_FILE))
        _fsync_directory(PIPELINE_STATE_FILE.parent)

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
    if not PIPELINE_STATE_FILE.exists():
        return None
    try:
        with locked_file(PIPELINE_STATE_FILE) as f:
            checkpoint = json.load(f)
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
    checkpoint_lock = PIPELINE_STATE_FILE.with_suffix(
        PIPELINE_STATE_FILE.suffix + ".lock"
    )
    with locked_file(checkpoint_lock, "a+", lock_type=fcntl.LOCK_EX):
        try:
            raw = PIPELINE_STATE_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw = ""
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
_REMOTE_PUBLICATION_CACHE = {
    "key": None,
    "checked_at": 0.0,
    "versions": frozenset(),
}


def _clear_remote_publication_cache():
    with _REMOTE_PUBLICATION_CACHE_LOCK:
        _REMOTE_PUBLICATION_CACHE.update({
            "key": None,
            "checked_at": 0.0,
            "versions": frozenset(),
        })


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
    now = time.monotonic()
    with _REMOTE_PUBLICATION_CACHE_LOCK:
        if (
            _REMOTE_PUBLICATION_CACHE.get("key") == cache_key
            and now - float(_REMOTE_PUBLICATION_CACHE.get("checked_at") or 0.0)
            <= 5.0
        ):
            return set(_REMOTE_PUBLICATION_CACHE.get("versions") or ())
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
    with _REMOTE_PUBLICATION_CACHE_LOCK:
        _REMOTE_PUBLICATION_CACHE.update({
            "key": cache_key,
            "checked_at": now,
            "versions": frozenset(verified),
        })
    return verified


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
                    and _official_parent_eligible(BOTS_DIR / d)
                ):
                    bots.append(d)
    return sorted(bots, key=version_sort_key)


def get_active_bots():
    """Return active bots and repair missing sentinels for trusted tagged bots."""

    return _discover_active_bots(repair_completed_sentinels=True)


def get_active_bots_read_only():
    """Return active bots without performing any filesystem repair.

    Read-only HTTP/catalog code must use this API so a GET request cannot create
    completion sentinels or otherwise mutate the evolution checkout.
    """

    return _discover_active_bots(repair_completed_sentinels=False)


def get_published_active_bots_read_only():
    """Return tagged active artifacts without requiring a local sentinel.

    View-only clones do not carry the gitignored ``.completed`` cache. Git tag,
    artifact, protocol, lifecycle, and official eligibility checks remain
    mandatory, so omitting that cache does not weaken completion authority.
    """

    return _discover_active_bots(
        repair_completed_sentinels=False,
        require_completed_sentinel=False,
    )


def _official_parent_eligible(bot_dir: Path) -> bool:
    try:
        spec = resolve_national_bot_spec(
            bot_dir,
            ROLE_PARENT_SOURCE,
            repo_root=BOTS_DIR.parent,
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


def find_current_v():
    """Return the immutable version-authority high-water.

    The archived completion/high-water tag namespace fixes the first strict
    target at v143.  Directory names and runtime sentinels are never numbering
    authority, so stale or abandoned worktrees cannot skip/reuse a version.
    """

    versions = {ARCHIVED_VERSION_HIGH_WATER}
    for tag in _git("tag", "-l", bot_tag_glob(), check=False).splitlines():
        version = parse_tag_version(tag.strip())
        if version is not None:
            versions.add(version)
    for tag in _git(
        "tag", "-l", "national-high-water-v*", check=False
    ).splitlines():
        match = re.fullmatch(r"national-high-water-v([1-9][0-9]*)", tag.strip())
        if match:
            versions.add(int(match.group(1)))
    return max(versions)


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
    try:
        return bool(_git("ls-files", "--", bot_relpath(version) + "/", check=False).strip())
    except Exception:
        return False


def find_max_committed_v():
    """Max version whose bot dir is git-tracked, regardless of tag/.completed.

    Whereas find_current_v() returns the latest *completed* (tagged) version,
    this returns the latest version whose code has landed in git at all —
    including bare commits bypassing commit_bot. prepare_generation() uses
    max(find_current_v(), find_max_committed_v()) + 1 as the next_v floor so a
    bare-committed version number is never regenerated/overwritten. Returns 0
    if no active-epoch bot dir is git-tracked.

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


def find_abandoned_version_floor():
    """Max abandoned version in the initialized strict policy epoch.

    Before the one-time reset, the file at this path belongs to the retired
    ``national_native_v1`` runtime.  It is audit material, not numbering
    authority; allowing it to reserve v143..v167 is how stale v155 state made
    status and prepare incorrectly announce v168.
    """
    try:
        from epoch_authority import policy_epoch_initialization

        if not policy_epoch_initialization()["initialized"]:
            return 0
    except Exception as exc:
        # Failure to prove epoch ownership must not promote mutable legacy data.
        log.warning("policy epoch initialization unavailable; abandoned floor ignored: %s", exc)
        return 0
    floor = 0
    try:
        if ABANDONED_VERSIONS_FILE.exists():
            with open(ABANDONED_VERSIONS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        version = json.loads(line).get("v")
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(version, int) and version > floor:
                        floor = version
    except Exception as exc:
        # If this file is unreadable, next_v may reuse an abandoned version. Make
        # that visible, but keep status/prepare reads best-effort.
        log.warning("abandoned_versions.jsonl unreadable; next_v floor may be stale: %s", exc)
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.abandoned_floor_unavailable",
                "warn",
                f"abandoned_versions.jsonl unreadable; next_v floor may reuse an abandoned version: {exc}",
                {"error": str(exc)[:200]},
            )
        except Exception:
            pass
    return floor


def compute_next_generation_v(current_v=None, max_committed_v=None, abandoned_floor=None):
    """Return the next generation version using the same floors as prepare_generation."""
    if current_v is None:
        current_v = find_current_v()
    if max_committed_v is None:
        max_committed_v = find_max_committed_v()
    if abandoned_floor is None:
        abandoned_floor = find_abandoned_version_floor()
    return max(int(current_v), int(max_committed_v), int(abandoned_floor)) + 1


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

    checkpoint_lock = PIPELINE_STATE_FILE.with_suffix(
        PIPELINE_STATE_FILE.suffix + ".lock"
    )
    with locked_file(checkpoint_lock, "a+", lock_type=fcntl.LOCK_EX):
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

    publication_lock = RESULTS_DIR / ".bot_publication.lock"
    with locked_file(publication_lock, "a+", lock_type=fcntl.LOCK_EX):
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
    archive_path = ARCHIVE_DIR / f"v{int(version)}.json"
    try:
        snapshot = json.loads(archive_path.read_text(encoding="utf-8"))
    except Exception:
        return False, "post_commit_archivist_archive_unavailable", None
    return _post_commit_archivist_receipt_validation(
        snapshot,
        version,
        source_v,
        require_pending=True,
    )


def consume_post_commit_archivist_receipt(version, source_v):
    """Atomically consume the one-shot post-commit Archivist handoff."""
    archive_path = ARCHIVE_DIR / f"v{int(version)}.json"
    try:
        with locked_file(archive_path, "r+", encoding="utf-8") as handle:
            snapshot = json.load(handle)
            ok, reason, receipt = _post_commit_archivist_receipt_validation(
                snapshot,
                version,
                source_v,
                require_pending=True,
            )
            if not ok:
                return False, reason, receipt
            receipt = dict(receipt)
            receipt["status"] = "consumed"
            receipt["consumed_at"] = time.time()
            snapshot["post_commit_archivist_receipt"] = receipt
            handle.seek(0)
            json.dump(snapshot, handle, indent=2, ensure_ascii=False)
            handle.truncate()
        return True, "", receipt
    except Exception as exc:
        return False, f"post_commit_archivist_consume_error:{type(exc).__name__}", None


def archive_rotate_files(version):
    """Rotate append-only data files by archiving old entries to archive/."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    rotation_rules = [
        (WORKER_FAILURES_FILE, 200),
        (MATCH_HISTORY_FILE, 500),
        (RATING_HISTORY_FILE, 100),
        (RESULTS_DIR / "events.jsonl", 1000),
    ]
    if LLM_COSTS_FILE.exists():
        rotation_rules.append((LLM_COSTS_FILE, 200))

    for filepath, keep_lines in rotation_rules:
        if not filepath.exists():
            continue
        with locked_file(filepath, "r") as f:
            lines = f.readlines()
        if len(lines) <= keep_lines:
            continue
        archived_lines = lines[:-keep_lines]
        hot_lines = lines[-keep_lines:]
        archive_name = f"{filepath.stem}_v{version}.jsonl"
        archive_path = ARCHIVE_DIR / archive_name
        with open(archive_path, "w") as f:
            f.writelines(archived_lines)
        with locked_file(filepath, "w") as f:
            f.writelines(hot_lines)
        # Preserve cost total when archiving LLM costs
        if filepath == LLM_COSTS_FILE:
            archived_cost = sum(
                json.loads(l).get("cost_usd", 0)
                for l in archived_lines if l.strip()
            )
            summary_file = ARCHIVE_DIR / "cost_summary.json"
            existing = 0.0
            if summary_file.exists():
                try:
                    existing = json.loads(summary_file.read_text()).get("grand_total", 0.0)
                except Exception:
                    pass
            summary_file.write_text(json.dumps({"grand_total": round(existing + archived_cost, 6)}))


def archive_old_logs(keep_generations=5):
    """Compress log directories older than keep_generations into .tar.gz."""
    current_v = find_current_v()
    cutoff_v = current_v - keep_generations
    if cutoff_v <= 0:
        return

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    for v in range(1, cutoff_v + 1):
        log_dir = RESULTS_DIR / f"v{v}" / "logs"
        if not log_dir.exists():
            continue
        archive_path = ARCHIVE_DIR / f"v{v}_logs.tar.gz"
        if archive_path.exists():
            shutil.rmtree(log_dir, ignore_errors=True)
            continue
        try:
            import tarfile
            parent_dir = RESULTS_DIR / f"v{v}"
            with tarfile.open(str(archive_path), "w:gz") as tar:
                tar.add(str(log_dir), arcname=f"v{v}/logs")
            shutil.rmtree(parent_dir, ignore_errors=True)
        except Exception:
            pass


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
