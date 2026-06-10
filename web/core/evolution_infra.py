"""Shared infrastructure for the poker bot evolution framework.

Contains constants, file utilities, git operations, ratings helpers.
No LLM agent logic — agent functions live in agent_*.py modules.
Runtime operations (daemon, LLM query, code verification) extracted to
daemon_management.py, llm_query.py, and code_verification.py.
"""

import os
import sys
import json
import logging
import shutil
import subprocess
import re
import asyncio
import fcntl
import time
import threading
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("pok.infra")

# Add workspace to sys.path for glicko2 import
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

MAX_ACTIVE_BOTS = 30

# Evaluation & quality thresholds
DAEMON_EVAL_TIMEOUT = 600
MIN_GAMES_FOR_EVAL = 100
MAX_LINES_PER_FILE = 1500       # Core strategy files (strategy.py, postflop.py)
MAX_LINES_HELPER = 1200         # All other .py files
CORE_STRATEGY_FILES = {"strategy.py", "postflop.py"}
MIN_DECISION_PASS_RATE = 0.7
MIN_CROSSOVER_DECISION_RATE = 0.6
MAX_WORKER_RETRIES = 4
MAX_MASTER_RETRIES = 3
MAX_CROSSOVER_RETRIES = 3
MAX_GENESIS_RETRIES = 3
WORKER_TIMEOUT = 1000         # Seconds before a hung worker call is aborted + retried
MAX_PARALLEL_WORKERS = 3      # Hard cap on simultaneous LLM worker calls (Semaphore)

# Prompt size limits — Sonnet supports 200K tokens (~800K chars); leave generous headroom
MAX_PROMPT_CHARS = 700_000

# Pipeline stage constants
STAGE_ORDER = ["prepared", "direction_audited", "master_planned", "workers_done", "quality_passed", "spot_verified", "reviewed", "critic_checked", "verified", "archived"]
STAGE_GATE_ALLOWLIST = {
    "prepared": set(),
    "direction_audited": set(),
    "master_planned": set(),
    "workers_done": set(),
    "quality_passed": {"quality"},
    "spot_verified": {"quality", "spot_verified"},
    "reviewed": {"quality", "spot_verified", "review"},
    "critic_checked": {"quality", "spot_verified", "review", "critic"},
    "verified": {"quality", "spot_verified", "review", "critic", "precommit_eval"},
    "archived": {"quality", "spot_verified", "review", "critic", "precommit_eval"},
}

EVOLUTION_BRANCH = "main"

# Watchdog: if no pipeline stage change occurs within this many seconds,
# the orchestrator watchdog will clear the session and restart from checkpoint.
WATCHDOG_TIMEOUT = 1200  # 20 minutes

# MCP servers to block for sub-agents (keep zai-mcp-server for vision, block the rest)
_BLOCKED_MCP_TOOLS = [
    "mcp__web-reader__webReader",
    "mcp__web-search-prime__web_search_prime",
    "mcp__zread__get_repo_structure",
    "mcp__zread__read_file",
    "mcp__zread__search_doc",
]

# Lazy-initialised semaphore — created on first use inside the event loop
_WORKER_SEMAPHORE: "asyncio.Semaphore | None" = None


def _get_worker_semaphore() -> "asyncio.Semaphore":
    """Return (creating if needed) the module-level worker concurrency semaphore."""
    global _WORKER_SEMAPHORE
    if _WORKER_SEMAPHORE is None:
        _WORKER_SEMAPHORE = asyncio.Semaphore(MAX_PARALLEL_WORKERS)
    return _WORKER_SEMAPHORE


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


def substitute_template(template, replacements):
    """Replace {key} placeholders in a template string. Warns on unreplaced placeholders."""
    result = template
    for key, value in replacements.items():
        result = result.replace(f"{{{key}}}", str(value))
    remaining = set(re.findall(r'\{([a-z_]+)\}', result))
    if remaining:
        log.warning("Unreplaced template placeholders: %s", remaining)
    return result


# ──────────────────────────────────────────────
# Pipeline Checkpoint (Process Recovery)
# ──────────────────────────────────────────────

def write_pipeline_checkpoint(next_v, source_v, stage, master_plan=None,
                               reviewer_feedback="", generation_attempt=0,
                               gate_results=None, worker_failure_count=None,
                               worker_invocation_count=None,
                               parent2_v=None, direction_audit=None,
                               audit_context=None):
    """Write pipeline stage checkpoint so a killed process can resume.

    Uses atomic tmp+rename under exclusive lock to prevent concurrent
    read-merge-write races (POSIX guarantees os.replace is atomic).
    """
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
        existing_parent2_v = parent2_v
        existing_direction_audit = None
        existing_audit_context = {}

        if existing and existing.get("next_v") == next_v and existing.get("source_v") == source_v:
            existing_gate_results = existing.get("gate_results", {}) or {}
            existing_failure_count = existing.get("worker_failure_count", existing.get("worker_invocation_count", 0))
            if master_plan is None:
                existing_master_plan = existing.get("master_plan")
            if not reviewer_feedback:
                existing_reviewer_feedback = existing.get("reviewer_feedback", "")
            if generation_attempt == 0:
                existing_generation_attempt = existing.get("generation_attempt", 0)
            if parent2_v is None:
                existing_parent2_v = existing.get("parent2_v")
            existing_direction_audit = existing.get("direction_audit")
            existing_audit_context = existing.get("audit_context", {}) or {}

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

        # Merge last_stage_change_ts: take max of existing vs current time.
        # This preserves the most recent genuine stage-change time on partial re-writes
        # (e.g. gate_results update without stage change).
        existing_stage_ts = 0.0
        if existing:
            existing_stage_ts = existing.get("last_stage_change_ts", 0.0)
        now_ts = time.time()
        # Only bump the timestamp if the stage actually changed
        old_stage = existing.get("stage") if existing else None
        new_stage_ts = now_ts if (old_stage != stage) else existing_stage_ts

        state = {
            "next_v": next_v, "source_v": source_v, "stage": stage,
            "master_plan": existing_master_plan, "reviewer_feedback": existing_reviewer_feedback,
            "generation_attempt": existing_generation_attempt,
            "worker_failure_count": existing_failure_count,
            "gate_results": existing_gate_results,
            "parent2_v": existing_parent2_v,
            "direction_audit": existing_direction_audit,
            "audit_context": existing_audit_context,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "last_stage_change_ts": new_stage_ts,
        }

        # Atomic write: tmp + fsync + rename, all under the same lock
        tmp = PIPELINE_STATE_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            f.write(json.dumps(state, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(PIPELINE_STATE_FILE))


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
    with locked_file(PIPELINE_STATE_FILE, "w", lock_type=fcntl.LOCK_EX) as f:
        # Truncate under lock, then unlink
        f.truncate(0)
    PIPELINE_STATE_FILE.unlink(missing_ok=True)


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
    marker = f"bots/claude_v{version}/"
    if marker in raw:
        return raw.split(marker, 1)[1]
    marker = f"claude_v{version}/"
    if marker in raw:
        return raw.split(marker, 1)[1]
    return raw.lstrip("./")


def get_active_bots():
    bots = []
    if BOTS_DIR.exists():
        for d in os.listdir(BOTS_DIR):
            if d.startswith("claude_v") and os.path.isdir(BOTS_DIR / d):
                if (BOTS_DIR / d / ".completed").exists():
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
    try:
        with locked_file(RATINGS_FILE, "r") as f:
            data = json.load(f)
        return {name: Glicko2Player.from_dict(d) for name, d in data.items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_daemon_stats():
    """Load daemon stats."""
    if STATS_FILE.exists():
        with locked_file(STATS_FILE, "r") as f:
            data = json.load(f)
        return data
    return {"pairs": {}, "total_periods": 0, "total_games": 0}


def _is_shutdown(event) -> bool:
    """Check if a shutdown signal is set. Accepts asyncio.Event, ShutdownManager, or None."""
    if event is None:
        return False
    if hasattr(event, 'is_set'):
        return event.is_set()
    if hasattr(event, 'is_shutting_down'):
        return event.is_shutting_down
    return False


async def wait_for_daemon_eval(bot_name, timeout=DAEMON_EVAL_TIMEOUT, min_games=MIN_GAMES_FOR_EVAL, ui=None, shutdown_event=None):
    """Wait for daemon to evaluate a new bot (async, non-blocking).

    Returns True when either:
      - games >= min_games (hard threshold), OR
      - rd < EVAL_RD_THRESHOLD and games >= EVAL_RD_MIN_GAMES (confidence-based early exit)
    Returns False on timeout or shutdown signal.
    """
    from daemon_management import daemon_proc, _daemon_lock

    EVAL_RD_THRESHOLD = 60
    EVAL_RD_MIN_GAMES = 20

    start = time.time()
    cached_bot_stats = None
    bot_stats_mtime = 0
    ratings_mtime = 0
    cached_rd = None
    last_log = start

    while time.time() - start < timeout:
        if _is_shutdown(shutdown_event):
            return False

        if BOT_STATS_FILE.exists():
            mt = os.path.getmtime(BOT_STATS_FILE)
            if mt != bot_stats_mtime:
                bot_stats_mtime = mt
                try:
                    with locked_file(BOT_STATS_FILE, "r") as f:
                        cached_bot_stats = json.load(f)
                except (json.JSONDecodeError, FileNotFoundError):
                    cached_bot_stats = {}
        if cached_bot_stats is None:
            cached_bot_stats = {}

        games = cached_bot_stats.get(bot_name, {}).get("games", 0)
        if games >= min_games:
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
                return True

        if ui and time.time() - last_log >= 30:
            elapsed = int(time.time() - start)
            rd_info = f", rd={cached_rd:.1f}" if cached_rd else ""
            ui.log_history(f"等待 {bot_name} 评估: {games}/{min_games} 场 ({elapsed}s{rd_info})", "info")
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
                return True
            else:
                if ui:
                    ui.log_history(f"Daemon 已终止 (rc={proc.returncode})，仅 {games}/{min_games} 场，等待重启...", "error")
                # Don't return False — daemon_monitor_thread may restart it.
                # Continue waiting until timeout expires.


        await asyncio.sleep(5)
    if ui:
        games = cached_bot_stats.get(bot_name, {}).get("games", 0)
        ui.log_history(f"评估超时 {bot_name}: 仅 {games}/{min_games} 场 ({int(time.time()-start)}s)", "warn")
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


def _git_ensure_main_branch():
    """Return to the canonical evolution branch (main) if an LLM drifted off it.

    LLM agents have Bash tool access and can accidentally run `git checkout -b`
    during their sessions. This guard detects and silently corrects the drift
    before any commit lands on the wrong branch.
    """
    current = _git("rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    if current == EVOLUTION_BRANCH:
        return
    if current == "HEAD":
        # Detached HEAD — reset to main
        log.warning("git: detached HEAD detected, resetting to %s", EVOLUTION_BRANCH)
        _git("checkout", EVOLUTION_BRANCH, check=False)
        return
    log.warning("git: on branch '%s', expected '%s'. Switching back before commit.",
                current, EVOLUTION_BRANCH)
    # Stash any uncommitted changes, switch to main, pop stash
    stash_out = _git("stash", check=False)
    _git("checkout", EVOLUTION_BRANCH, check=False)
    if "No local changes to save" not in stash_out:
        pop_out = _git("stash", "pop", check=False)
        if "error" in pop_out.lower():
            log.warning("git: stash pop failed: %s", pop_out[:200])


def git_has_tag(version):
    """Check if a bot-v{version} tag exists (authoritative completion proof)."""
    return bool(_git("tag", "-l", f"bot-v{version}", check=False).strip())


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
    _git("add", f"bots/claude_v{version}", check=False)
    if EXPERIENCE_FILE.exists():
        _git("add", str(EXPERIENCE_FILE.relative_to(PROJECT_ROOT)), check=False)
    _git("commit", "-m", msg)
    tag = f"bot-v{version}"
    _git("tag", "-d", tag, check=False)
    _git("tag", tag, "-m", f"Bot v{version}: {strategy_tag}")

    push_ok = False
    if os.environ.get("EVOLUTION_GIT_PUSH") == "1":
        _git("push", "origin", "main", check=False)
        _git("push", "origin", tag, check=False)
        push_ok = True
    return push_ok


def git_get_parent(version):
    """从 tag/commit message 解析 parent。"""
    tag = f"bot-v{version}"
    tags = _git("tag", "-l", tag, check=False)
    if tags:
        msg = _git("for-each-ref", f"refs/tags/{tag}", "--format=%(contents)")
    else:
        log = _git("log", "--diff-filter=A", "--oneline", "-1", "--",
                    f"bots/claude_v{version}/", check=False)
        if not log:
            return None
        commit_hash = log.split()[0]
        msg = _git("show", "-s", "--format=%B", commit_hash, check=False)
    for line in (msg or "").split("\n"):
        if line.strip().startswith("parent:"):
            return line.split(":", 1)[1].strip()
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
    run_decision_test_details, seed_initial_bots,
)
