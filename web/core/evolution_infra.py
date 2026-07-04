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
from experience_pool import trim_experience_pool

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────

CORE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CORE_DIR.parent.parent
_COPY_IGNORE = shutil.ignore_patterns('__pycache__', '*.pyc')
PROMPTS_DIR = CORE_DIR / "prompts"
RESULTS_DIR = CORE_DIR / "results"
BOTS_DIR = PROJECT_ROOT / "bots"
EXPERIENCE_FILE = CORE_DIR / "experience_pool.md"
REFERENCE_DIR = CORE_DIR / "reference_bots"
GRAVEYARD_DIR = BOTS_DIR / "graveyard"
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
# fix-5: cross-gen direction pivot — tracks exhausted directions per generation
# so consecutive same-axis exhaustion can force a structural pivot.
CROSS_GEN_EXHAUSTED_HISTORY = RESULTS_DIR / "cross_gen_exhausted_history.jsonl"

MAX_ACTIVE_BOTS = 30

# Evaluation & quality thresholds
DAEMON_EVAL_TIMEOUT = 600
MIN_GAMES_FOR_EVAL = 100
EVAL_WAIT_PROGRESS_INTERVAL_SEC = int(os.environ.get("POK_EVAL_WAIT_PROGRESS_INTERVAL_SEC", "30"))
MAX_LINES_PER_FILE = 2000       # Core strategy files (strategy.py, postflop.py) — base limit
MAX_LINES_HELPER = 1500         # All other .py files — base limit
MAX_LINES_HARD_CAP = 2500       # Hard cap: no .py file may exceed this, even with adaptive budget
LINE_GROWTH_BUDGET = 0.15       # Adaptive limit = max(base, source_lines * (1 + budget))

# Strip a trailing status annotation the LLM may append to target_files entries,
# e.g. "bet_size_profile.py (NEW)" or "strategy.py [CREATE]". Requires a BARE
# keyword inside the brackets so legitimate names like "report(2).py" survive.
_TARGET_ANNOTATION_RE = re.compile(
    r"\s*[\(\[](?:NEW|CREATE|DELETE|MODIFIED)[\)\]]\s*$", re.IGNORECASE
)
CORE_STRATEGY_FILES = {"strategy.py", "postflop.py"}
MIN_DECISION_PASS_RATE = 0.7
MIN_CROSSOVER_DECISION_RATE = 0.6
MAX_WORKER_RETRIES = 4
MAX_MASTER_RETRIES = 3
MAX_CROSSOVER_RETRIES = 3
MAX_GENESIS_RETRIES = 3
MAX_PRECOMMIT_RETRIES = 3   # Max run_precommit_eval attempts against the SAME bot code (resets on worker rework)
MAX_PRECOMMIT_REWORK_ROUNDS = int(os.environ.get("POK_MAX_PRECOMMIT_REWORK_ROUNDS", "3"))
MAX_MASTER_AUDIT_RETRIES = 2  # Master plan audit re-plan cap (prevents bug #6b retry loop)
MAX_GEN_COST = 7.0            # Per-cycle LLM cost cap (safety net above normal 4-attempt retry budget ~$5-7)
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
    is_rework_reset_transition,
)

EVOLUTION_BRANCH = "main"

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


@contextmanager
def locked_file(path, mode='r', lock_type=None, encoding=None):
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
        entry["win_rate"] = round(entry["wins"] / entry["games"], 4)


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

def _capture_repo_baseline(stage):
    """Capture the git baseline persisted with an active generation checkpoint."""
    try:
        from repo_state import git_worktree_snapshot
        snapshot = git_worktree_snapshot()
        return {
            "branch": snapshot.get("branch", ""),
            "head": snapshot.get("head", ""),
            "entry_count": snapshot.get("entry_count", 0),
            "dirty_count": snapshot.get("dirty_count", 0),
            "untracked_count": snapshot.get("untracked_count", 0),
            "entries": (snapshot.get("entries") or [])[:40],
            "truncated": bool(snapshot.get("truncated")),
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
            "captured_stage": stage,
            "captured_ts": time.time(),
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


def write_pipeline_checkpoint(next_v, source_v, stage, master_plan=None,
                               reviewer_feedback="", generation_attempt=0,
                               gate_results=None, worker_failure_count=None,
                               worker_invocation_count=None,
                               parent2_v=None, direction_audit=None,
                               audit_context=None, reset_generation_attempt=False,
                               audit_attempt=None, reset_audit_attempt=False,
                               precommit_attempt=None, reset_precommit_attempt=False,
                               precommit_rework_count=None,
                               timeout_extensions=None, touch_stage_timestamp=False,
                               literature_probe=None):
    """Write pipeline stage checkpoint so a killed process can resume.

    Uses atomic tmp+rename under exclusive lock to prevent concurrent
    read-merge-write races (POSIX guarantees os.replace is atomic).
    """
    try:
        from workflow_profiles import get_workflow_profile
        _profile = get_workflow_profile()
        current_workflow_profile_id = getattr(_profile, "profile_id", "")
        current_national_execution_mode = getattr(_profile, "national_execution_mode", "adapter")
    except Exception:
        current_workflow_profile_id = ""
        current_national_execution_mode = ""

    # Single exclusive lock covers read-merge-write-rename to prevent TOCTOU
    with locked_file(PIPELINE_STATE_FILE, "a+", lock_type=fcntl.LOCK_EX) as f:
        f.seek(0)
        raw = f.read()
        existing = None
        if raw.strip():
            try:
                existing = json.loads(raw)
            except Exception:
                existing = None

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
        existing_timeout_extensions = 0
        existing_literature_probe = None
        existing_repo_baseline = None

        if existing and existing.get("next_v") == next_v and existing.get("source_v") == source_v:
            existing_gate_results = existing.get("gate_results", {}) or {}
            existing_failure_count = existing.get("worker_failure_count", 0)
            existing_timeout_extensions = existing.get("timeout_extensions", 0)
            if master_plan is None:
                existing_master_plan = existing.get("master_plan")
            if not reviewer_feedback:
                existing_reviewer_feedback = existing.get("reviewer_feedback", "")
            if generation_attempt == 0:
                existing_generation_attempt = existing.get("generation_attempt", 0)
            if audit_attempt is None:
                existing_audit_attempt = existing.get("audit_attempt", 0)
            if precommit_attempt is None:
                existing_precommit_attempt = existing.get("precommit_attempt", 0)
            if precommit_rework_count is None:
                existing_precommit_rework_count = existing.get("precommit_rework_count", 0)
            if timeout_extensions is not None:
                existing_timeout_extensions = int(timeout_extensions)
            if parent2_v is None:
                existing_parent2_v = existing.get("parent2_v")
            existing_direction_audit = existing.get("direction_audit")
            existing_audit_context = existing.get("audit_context", {}) or {}
            existing_literature_probe = existing.get("literature_probe")
            existing_repo_baseline = existing.get("repo_baseline")
        elif existing:
            active_stage = existing.get("stage")
            dead_stages = {None, "timed_out", "infra_timed_out", "archived", "abandoned"}
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
        if worker_failure_count is not None:
            existing_failure_count = worker_failure_count
        elif worker_invocation_count is not None:
            existing_failure_count = worker_invocation_count
        if direction_audit is not None:
            existing_direction_audit = direction_audit
        if audit_context is not None:
            existing_audit_context.update(audit_context)
        if literature_probe is not None:
            existing_literature_probe = literature_probe

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
        refresh_repo_baseline = is_rework_reset_transition(old_stage, stage)
        if refresh_repo_baseline:
            existing_precommit_attempt = 0
            existing_timeout_extensions = 0

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
        run_id = f"{next_v}#{existing_generation_attempt}"
        if refresh_repo_baseline:
            existing_repo_baseline = _capture_repo_baseline(stage)
        elif not existing_repo_baseline:
            existing_repo_baseline = _capture_repo_baseline(stage)

        state = {
            "next_v": next_v, "source_v": source_v, "stage": stage,
            "run_id": run_id,
            "master_plan": existing_master_plan, "reviewer_feedback": existing_reviewer_feedback,
            "generation_attempt": existing_generation_attempt,
            "audit_attempt": existing_audit_attempt,
            "precommit_attempt": existing_precommit_attempt,
            "precommit_rework_count": existing_precommit_rework_count,
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
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "last_stage_change_ts": new_stage_ts,
            "last_update_ts": now_ts,  # Always bumps on any checkpoint write
        }

        # Atomic write: tmp + fsync + rename, all under the same lock
        tmp = PIPELINE_STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            f.write(json.dumps(state, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(PIPELINE_STATE_FILE))

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
            return json.load(f)
    except Exception:
        return None


def clear_pipeline_checkpoint():
    """Delete pipeline checkpoint (called on successful commit).

    Uses exclusive lock to prevent race with concurrent writes.
    """
    if not PIPELINE_STATE_FILE.exists():
        return
    previous = None
    with locked_file(PIPELINE_STATE_FILE, "a+", lock_type=fcntl.LOCK_EX) as f:
        f.seek(0)
        raw = f.read()
        if raw.strip():
            try:
                previous = json.loads(raw)
            except Exception:
                previous = None
        # Truncate under lock, then unlink — both inside the lock to prevent TOCTOU
        f.seek(0)
        f.truncate(0)
        PIPELINE_STATE_FILE.unlink(missing_ok=True)
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
    def update_metrics(self, metrics): pass
    def emit_tool_call(self, tool_name: str, args: dict, role: str = ""): pass


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
    primary = BOTS_DIR / f"claude_v{version}"
    if primary.exists():
        return primary
    graveyard = GRAVEYARD_DIR / f"claude_v{version}"
    if graveyard.exists():
        return graveyard
    return primary


def get_logs_dir(version):
    d = RESULTS_DIR / f"v{version}" / "logs"
    os.makedirs(d, exist_ok=True)
    return d


def _target_rel(path, version):
    raw = str(path).strip()
    if not raw:
        return ""
    raw = raw.replace("\\", "/")
    raw = _TARGET_ANNOTATION_RE.sub("", raw).strip()
    # 循环剥离任意层 bots/claude_v{N}/ 前缀（含 source_v + 双重嵌套）。
    # root-cause-audit 2026-06-21: Master context (agent_master.py:100) 注入
    # bots/claude_v{source_v}/ 路径，worker 非确定性地把它写进 target_files，甚至双重嵌套
    # bots/claude_v{src}/bots/claude_v{src}/... (gen132 3×, gen137 4×)。单层正则只剥一层仍残留；
    # 循环剥离直到无版本前缀。
    while True:
        m = re.match(r'(?:\./)?(?:bots/)?claude_v\d+/(.+)$', raw)
        if not m:
            break
        raw = m.group(1)
    return raw


def get_active_bots():
    """Active bots = those with BOTH a .completed sentinel AND a git tag.

    Trust model mirrors find_current_v(): the git tag 'bot-v{N}' is the single
    authoritative completion proof. A bare .completed file (written by prepare
    or left behind by a crashed/never-committed generation) is NOT trusted —
    it is exactly how a "ghost bot" like v107 (completed-but-untagged) leaked
    into find_latest_active_v() and was used as an evolution source.

    Collecting all tags once here (instead of calling git_has_tag per bot)
    keeps this O(1 git call) regardless of bot count.
    """
    tag_versions = set()
    for tag in _git("tag", "-l", "bot-v*", check=False).strip().splitlines():
        try:
            tag_versions.add(int(tag.replace("bot-v", "")))
        except ValueError:
            pass

    bots = []
    if BOTS_DIR.exists():
        for d in os.listdir(BOTS_DIR):
            if d.startswith("claude_v") and os.path.isdir(BOTS_DIR / d):
                if (BOTS_DIR / d / ".completed").exists():
                    try:
                        v = int(d.split("_v")[1])
                    except (ValueError, IndexError):
                        continue
                    if v in tag_versions:  # git tag backs the .completed sentinel
                        bots.append(d)
    return sorted(bots, key=lambda x: int(x.split("_v")[1]))


def find_current_v():
    """Find the latest completed bot version.

    Cascading sources: git tags > .completed sentinel files (backed by tag) > directory names.
    .completed files without a corresponding git tag are NOT trusted as complete.
    """
    versions = set()
    tag_versions = set()

    # Source 1: git tags (most authoritative)
    tags = _git("tag", "-l", "bot-v*", check=False).strip().splitlines()
    for tag in tags:
        try:
            v = int(tag.replace("bot-v", ""))
            versions.add(v)
            tag_versions.add(v)
        except ValueError:
            pass

    # Source 2: .completed sentinel files — only trust if backed by a git tag
    if BOTS_DIR.exists():
        for d in os.listdir(BOTS_DIR):
            if d.startswith("claude_v") and (BOTS_DIR / d / ".completed").exists():
                try:
                    v = int(d.split("_v")[1])
                    if v in tag_versions:
                        versions.add(v)
                except (ValueError, IndexError):
                    pass

    if versions:
        return max(versions)

    # Source 3: any claude_v* directory (fallback for version numbering only)
    if BOTS_DIR.exists():
        for d in os.listdir(BOTS_DIR):
            if d.startswith("claude_v") and os.path.isdir(BOTS_DIR / d):
                try:
                    versions.add(int(d.split("_v")[1]))
                except (ValueError, IndexError):
                    pass

    return max(versions) if versions else 0


def find_latest_active_v():
    """Find the highest version among ACTIVE bots (not graveyard).
    Returns 0 if no active bots exist.
    """
    active = get_active_bots()
    if not active:
        return 0
    return max(int(b.split("_v")[1]) for b in active)


# ──────────────────────────────────────────────
# Ratings
# ──────────────────────────────────────────────

def load_ratings():
    """Load Glicko-2 ratings with shared lock."""
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


def git_push_refs(*refs: str) -> bool:
    """Push refs to origin and return the real aggregate result."""
    if not refs:
        return True
    ok = True
    errors = []
    for ref in refs:
        try:
            _git("push", "origin", ref)
        except Exception as exc:
            ok = False
            errors.append({"ref": ref, "error": str(exc)[:500]})
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
            {"refs": list(refs), "ok": ok, "errors": errors},
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
    """Check if a bot-v{version} tag exists (authoritative completion proof)."""
    return bool(_git("tag", "-l", f"bot-v{version}", check=False).strip())


def git_dir_is_committed(version):
    """True if bots/claude_v{version}/ has any git-tracked file.

    Detects BARE COMMITS — code that landed in git via a direct `git commit`
    (e.g. an LLM running git in Bash) but was never finalized through commit_bot,
    so it lacks both the bot-v{N} tag and the .completed sentinel. This is the
    root-cause signal of the v117 repeated-regeneration loop (2026-06-18): v117
    was bare-committed twice (f6bcccf/f6c4eb7) without a tag, so find_current_v()
    kept returning 116 and the orchestrator regenerated v117 five times until
    commit_bot finally tagged it (20db34c, 22:02). 'git ls-files' is the test:
    a directory with on-disk files but no tracked files is an untracked scratch
    dir (safe to overwrite); a directory with tracked files is committed state.
    """
    try:
        return bool(_git("ls-files", "--", f"bots/claude_v{version}/", check=False).strip())
    except Exception:
        return False


def find_max_committed_v():
    """Max version whose bot dir is git-tracked, regardless of tag/.completed.

    Whereas find_current_v() returns the latest *completed* (tagged) version,
    this returns the latest version whose code has landed in git at all —
    including bare commits bypassing commit_bot. prepare_generation() uses
    max(find_current_v(), find_max_committed_v()) + 1 as the next_v floor so a
    bare-committed version number is never regenerated/overwritten. Returns 0
    if no claude_v* dir is git-tracked.

    Implementation: a SINGLE `git ls-files bots/claude_v*` call (not one
    subprocess per directory) keeps this O(1 git call)/generation regardless
    of how many bot dirs (~125 incl. graveyard) exist.
    """
    try:
        out = _git("ls-files", "--", "bots/claude_v*", check=False)
    except Exception:
        return 0
    max_v = 0
    for line in out.splitlines():
        # line like "bots/claude_v117/card_utils.py" — extract the dir version
        parts = line.split("/")
        if len(parts) < 2 or not parts[1].startswith("claude_v"):
            continue
        try:
            v = int(parts[1].split("_v")[1])
        except (ValueError, IndexError):
            continue
        if v > max_v:
            max_v = v
    return max_v


def find_abandoned_version_floor():
    """Max abandoned bot version that must not be reused for a future generation."""
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


def git_commit_bot(version, source_v, strategy_tag, rating_info="", parent2_v=None):
    """Commit a completed bot generation.

    Always commits on EVOLUTION_BRANCH (main). Calls _git_ensure_main_branch()
    first so that LLM-created side-branches never pollute the evolution history.
    Stage only the evolved bot and curated learning notes; daemon/result churn
    must not leak into evolution commits.
    """
    _git_ensure_main_branch()
    parent_line = f"parent: claude_v{source_v}"
    if parent2_v is not None:
        parent_line += f"\nparent2: claude_v{parent2_v}"
    msg = (
        f"evolve: v{source_v} → v{version}\n\n"
        f"{parent_line}\n"
        f"strategy: {strategy_tag}\n"
        f"{rating_info}"
    )
    preexisting_staged = [
        p for p in _git("diff", "--cached", "--name-only", check=False).splitlines()
        if p
    ]
    if preexisting_staged:
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.git_commit_blocked_preexisting_staged",
                "error",
                f"v{version}: refusing commit because unrelated staged files already exist",
                {"version": version, "staged_files": preexisting_staged[:40]},
            )
        except Exception:
            pass
        raise RuntimeError(
            "Refusing git_commit_bot with pre-existing staged files: "
            + ", ".join(preexisting_staged[:10])
        )

    # LOG GAP FIX (2026-06-29): record what gets staged so a hand-edit bypass
    # (orchestrator LLM mutating bot code outside execute_workers) is visible.
    bot_path = f"bots/claude_v{version}"
    _staged = _git("add", "--", bot_path, check=False)
    _exp_added = False
    allowed_paths = [bot_path]
    if EXPERIENCE_FILE.exists():
        exp_rel = str(EXPERIENCE_FILE.relative_to(PROJECT_ROOT))
        _git("add", "--", exp_rel, check=False)
        _exp_added = True
        allowed_paths.append(exp_rel)
    # Capture the staged file list right before commit for auditability.
    _staged_files = _git("diff", "--cached", "--name-only", check=False).strip().splitlines()
    allowed_exact = set(allowed_paths)
    allowed_prefixes = [p.rstrip("/") + "/" for p in allowed_paths if p.endswith(f"claude_v{version}")]
    unexpected_staged = [
        p for p in _staged_files
        if p not in allowed_exact and not any(p.startswith(prefix) for prefix in allowed_prefixes)
    ]
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
            f"v{version}: staging {len(_staged_files)} file(s) for commit",
            {"version": version, "source_v": source_v,
             "staged_files": _staged_files[:30],
             "experience_added": _exp_added},
        )
    except Exception:
        pass
    _git("commit", "-m", msg, "--", *allowed_paths)
    _commit_hash = _git("rev-parse", "HEAD", check=False).strip()[:12]
    tag = f"bot-v{version}"
    _git("tag", "-d", tag, check=False)
    _git("tag", tag, "-m", f"Bot v{version}: {strategy_tag}")
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
    if os.environ.get("EVOLUTION_GIT_PUSH") == "1":
        push_ok = git_push_refs("main", tag)
    return push_ok


def git_get_parent(version):
    """从 tag/commit message 解析 parent。"""
    tag = f"bot-v{version}"
    tags = _git("tag", "-l", tag, check=False)
    if tags:
        commit_hash = _git("rev-list", "-n", "1", tag, check=False).strip()
        if not commit_hash:
            return None
        msg = _git("show", "-s", "--format=%B", commit_hash, check=False)
    else:
        log = _git("log", "--diff-filter=A", "--oneline", "-1", "--",
                    f"bots/claude_v{version}/", check=False)
        if not log:
            return None
        commit_hash = log.split()[0]
        msg = _git("show", "-s", "--format=%B", commit_hash, check=False)
    for line in (msg or "").split("\n"):
        if line.strip().startswith("parent:"):
            parent = line.split(":", 1)[1].strip()
            try:
                return int(parent.replace("claude_v", "").replace("v", ""))
            except ValueError:
                return parent
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
        "git_tag": f"bot-v{version}",
    }

    try:
        snapshot["git_commit"] = _git("rev-parse", "--short", f"bot-v{version}", check=False)
    except Exception:
        pass

    ratings = load_ratings()
    p = ratings.get(f"claude_v{version}")
    if p:
        snapshot["rating"] = {"r": round(p.r, 1), "rd": round(p.rd, 1)}

    try:
        from tool_helpers import compute_h2h_avg_winrate, _load_h2h_data
        h2h_wr = compute_h2h_avg_winrate(f"claude_v{version}", _load_h2h_data())
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
        diff_stat = _git("diff", "--stat", f"bot-v{source_v}..bot-v{version}",
                         "--", f"bots/claude_v{version}/", check=False)
        if diff_stat:
            last_line = diff_stat.strip().split("\n")[-1]
            snapshot["diff_stats_raw"] = last_line.strip()
    except Exception:
        pass

    snapshot["pool_size"] = len(get_active_bots())

    archive_path = ARCHIVE_DIR / f"v{version}.json"
    with open(archive_path, "w") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    return snapshot


def archive_rotate_files(version):
    """Rotate append-only data files by archiving old entries to archive/."""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    rotation_rules = [
        (WORKER_FAILURES_FILE, 200),
        (MATCH_HISTORY_FILE, 500),
        (RATING_HISTORY_FILE, 100),
        (None, 1000),  # placeholder — resolved below
    ]
    from system_log import SYSTEM_EVENTS_FILE
    rotation_rules[3] = (SYSTEM_EVENTS_FILE, 1000)
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
    run_import_contract_test, seed_initial_bots,
)
