"""Shared infrastructure for the poker bot evolution framework.

Contains constants, file utilities, git operations, daemon management,
ratings helpers, LLM query primitives, and code verification tools.
No LLM agent logic — agent functions live in agent_*.py modules.
"""

import os
import sys
import json
import shutil
import subprocess
import re
import signal
import asyncio
import fcntl
import atexit
import time
import threading
from contextlib import contextmanager
from pathlib import Path

from claude_agent_sdk import (
    query as claude_query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ThinkingBlock,
    CLINotFoundError,
    ProcessError,
)

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
MAX_LINES_PER_FILE = 1000
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
STAGE_ORDER = ["prepared", "workers_done", "quality_passed", "reviewed", "critic_checked", "verified", "archived"]
STAGE_GATE_ALLOWLIST = {
    "prepared": set(),
    "workers_done": set(),
    "quality_passed": {"quality"},
    "reviewed": {"quality", "review"},
    "critic_checked": {"quality", "review", "critic"},
    "verified": {"quality", "review", "critic", "precommit_eval"},
    "archived": {"quality", "review", "critic", "precommit_eval"},
}

EVOLUTION_BRANCH = "main"

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


def _is_rate_limited(output: str) -> bool:
    return "529" in output or "该模型当前访问量过大" in output or "rate limit" in output.lower()

# Add workspace to sys.path for glicko2 import
from glicko2 import Glicko2Player, update_rating_period
from experience_pool import trim_experience_pool

# Global daemon process handle
daemon_proc = None
_daemon_lock = threading.Lock()
_atexit_registered = False


# ──────────────────────────────────────────────
# Utility Functions
# ──────────────────────────────────────────────

def _get_worker_semaphore() -> "asyncio.Semaphore":
    """Return (creating if needed) the module-level worker concurrency semaphore."""
    global _WORKER_SEMAPHORE
    if _WORKER_SEMAPHORE is None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        _WORKER_SEMAPHORE = asyncio.Semaphore(MAX_PARALLEL_WORKERS)
    return _WORKER_SEMAPHORE


def _trim_to_budget(text: str, max_chars: int, tail: bool = False) -> str:
    """Trim text to max_chars. If tail=True, keep the LAST max_chars (most recent content)."""
    if len(text) <= max_chars:
        return text
    note = "\n...[TRIMMED]\n"
    if tail:
        return note + text[-(max_chars - len(note)):]
    return text[:max_chars - len(note)] + note


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
    with open(path, actual_mode, **open_kwargs) as f:
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
        print(f"[WARN] Unreplaced template placeholders: {remaining}")
    return result


# ──────────────────────────────────────────────
# Pipeline Checkpoint (Process Recovery)
# ──────────────────────────────────────────────

def write_pipeline_checkpoint(next_v, source_v, stage, master_plan=None,
                               reviewer_feedback="", generation_attempt=0,
                               gate_results=None, worker_invocation_count=None,
                               parent2_v=None):
    """Write pipeline stage checkpoint so a killed process can resume.

    Uses atomic tmp+rename: if the process crashes mid-write, the old file
    survives intact (POSIX guarantees os.replace is atomic).
    """
    # Read existing state under shared lock
    existing = None
    if PIPELINE_STATE_FILE.exists():
        try:
            with locked_file(PIPELINE_STATE_FILE, "r") as f:
                raw = f.read()
                if raw.strip():
                    existing = json.loads(raw)
        except Exception:
            existing = None

    # Merge with existing — preserve gate_results, master_plan, etc.
    existing_gate_results = {}
    existing_invocation_count = 0
    existing_master_plan = master_plan
    existing_reviewer_feedback = reviewer_feedback
    existing_generation_attempt = generation_attempt
    existing_parent2_v = parent2_v

    if existing and existing.get("next_v") == next_v and existing.get("source_v") == source_v:
        existing_gate_results = existing.get("gate_results", {}) or {}
        existing_invocation_count = existing.get("worker_invocation_count", 0)
        if master_plan is None:
            existing_master_plan = existing.get("master_plan")
        if not reviewer_feedback:
            existing_reviewer_feedback = existing.get("reviewer_feedback", "")
        if generation_attempt == 0:
            existing_generation_attempt = existing.get("generation_attempt", 0)
        if parent2_v is None:
            existing_parent2_v = existing.get("parent2_v")

    if gate_results:
        existing_gate_results.update(gate_results)
    if worker_invocation_count is not None:
        existing_invocation_count = worker_invocation_count

    state = {
        "next_v": next_v, "source_v": source_v, "stage": stage,
        "master_plan": existing_master_plan, "reviewer_feedback": existing_reviewer_feedback,
        "generation_attempt": existing_generation_attempt,
        "worker_invocation_count": existing_invocation_count,
        "gate_results": existing_gate_results,
        "parent2_v": existing_parent2_v,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    # Atomic write: tmp under exclusive lock + os.replace
    tmp = PIPELINE_STATE_FILE.with_suffix(".tmp")
    with locked_file(tmp, "w") as tf:
        json.dump(state, tf, indent=2)
        tf.flush()
        os.fsync(tf.fileno())
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
    """Delete pipeline checkpoint (called on successful commit)."""
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
    """Find the latest completed bot version from git tags (authoritative)."""
    tags = _git("tag", "-l", "bot-v*", check=False).strip().splitlines()
    if not tags:
        return 6  # seeded bots v1-v6 have no tags
    versions = []
    for tag in tags:
        try:
            versions.append(int(tag.replace("bot-v", "")))
        except ValueError:
            pass
    return max(versions) if versions else 6


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


async def wait_for_daemon_eval(bot_name, timeout=DAEMON_EVAL_TIMEOUT, min_games=MIN_GAMES_FOR_EVAL, ui=None, shutdown_event: asyncio.Event | None = None):
    """Wait for daemon to evaluate a new bot (async, non-blocking).

    Returns True when either:
      - games >= min_games (hard threshold), OR
      - rd < EVAL_RD_THRESHOLD and games >= EVAL_RD_MIN_GAMES (confidence-based early exit)
    Returns False on timeout or shutdown signal.
    """
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
        await asyncio.sleep(5)
    if ui:
        games = cached_bot_stats.get(bot_name, {}).get("games", 0)
        ui.log_history(f"评估超时 {bot_name}: 仅 {games}/{min_games} 场 ({int(time.time()-start)}s)", "warn")
    return False


# ──────────────────────────────────────────────
# Daemon Management
# ──────────────────────────────────────────────

def _drain_stdout(proc):
    """Drain daemon stdout to prevent pipe buffer deadlock."""
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            print(f"[DAEMON] {line.rstrip()}")
    except (ValueError, OSError):
        pass  # Pipe closed


def start_daemon(workers=14, pairs=5):
    """Start elo_daemon.py as a background subprocess in its own process group."""
    global daemon_proc, _atexit_registered
    with _daemon_lock:
        if daemon_proc and daemon_proc.poll() is None:
            return daemon_proc  # Already running
        # Kill orphaned daemon from a previous process
        daemon_pid_file = RESULTS_DIR / ".daemon_pid"
        if daemon_pid_file.exists():
            try:
                old_pid = int(daemon_pid_file.read_text().strip())
                try:
                    os.killpg(os.getpgid(old_pid), signal.SIGTERM)
                    time.sleep(1)
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
        daemon_pid_file.write_text(str(daemon_proc.pid))
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
    global daemon_proc
    with _daemon_lock:
        if daemon_proc is None:
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
                daemon_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    if pgid is not None:
                        os.killpg(pgid, signal.SIGKILL)
                    else:
                        daemon_proc.kill()
                except (ProcessLookupError, PermissionError):
                    daemon_proc.kill()
                try:
                    daemon_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        daemon_proc = None
        # Clean up PID file
        daemon_pid_file = RESULTS_DIR / ".daemon_pid"
        daemon_pid_file.unlink(missing_ok=True)
    from system_log import log_system_event
    log_system_event("daemon.stopped", "info", "Daemon stopped")


def daemon_monitor_thread(ui, stop_event, daemon_workers=14, daemon_pairs=5):
    """Background thread: reads daemon stats, updates UI, auto-restarts dead daemon."""
    if not ui:
        return
    restart_count = 0
    while not stop_event.is_set():
        try:
            with _daemon_lock:
                proc = daemon_proc
            if proc is not None and proc.poll() is not None:
                restart_count += 1
                if restart_count > 5:
                    ui.log_history("Daemon failed 5x consecutively, stopping auto-restart", "error")
                    from system_log import log_system_event
                    log_system_event("daemon.crashed", "error", f"Daemon failed {restart_count}x, auto-restart stopped",
                                     {"restart_count": restart_count})
                    break
                backoff = min(3 * (2 ** (restart_count - 1)), 120)
                ui.log_history(f"⚠️ Daemon exited, restarting in {backoff}s (attempt {restart_count})", "warn")
                from system_log import log_system_event
                log_system_event("daemon.crashed", "error", f"Daemon exited, restarting (attempt {restart_count})",
                                 {"restart_count": restart_count})
                if stop_event.wait(backoff):
                    break
                start_daemon(workers=daemon_workers, pairs=daemon_pairs)
            else:
                restart_count = 0
            stats = load_daemon_stats()
            ratings = load_ratings()
            ui.update_daemon_status(stats, ratings)
        except Exception as e:
            ui.log_history(f"Daemon monitor error: {e}", "error")
        stop_event.wait(3)


# ──────────────────────────────────────────────
# Git Helpers
# ──────────────────────────────────────────────

def _git(*args, check=True):
    """Run git command, return stdout."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=str(PROJECT_ROOT),
        capture_output=True, text=True
    )
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
        print(f"[git] WARNING: detached HEAD detected, resetting to {EVOLUTION_BRANCH}")
        _git("checkout", EVOLUTION_BRANCH, check=False)
        return
    print(f"[git] WARNING: on branch '{current}', expected '{EVOLUTION_BRANCH}'. "
          f"Switching back before commit.")
    # Stash any uncommitted changes, switch to main, pop stash
    stash_out = _git("stash", check=False)
    _git("checkout", EVOLUTION_BRANCH, check=False)
    if "No local changes to save" not in stash_out:
        pop_out = _git("stash", "pop", check=False)
        if "error" in pop_out.lower():
            print(f"[git] WARNING: stash pop failed: {pop_out[:200]}")


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
            snapshot["review_score"] = gate_results["review"].get("quality_score", 0)
        if gate_results.get("critic"):
            snapshot["critic_score"] = gate_results["critic"].get("score", 0)
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
# LLM Query Primitive
# ──────────────────────────────────────────────

async def run_claude_query(prompt, context_files, ui, role_name, log_file_path, model="sonnet", tools=None):
    """Run a Claude query via the Agent SDK with cost tracking and typed streaming.

    tools: list of built-in tool names (e.g. ["Bash", "Read"]) or a ToolsPreset dict.
           When None, no built-in tools are exposed to the model.
    """
    # Build (path, content) pairs for context files
    context_parts = []
    if context_files:
        for cf in context_files:
            if os.path.exists(cf):
                with open(cf, 'r') as f:
                    context_parts.append((cf, f.read()))

    # Assemble prompt with context files, smart-budgeting if needed
    if context_parts:
        ctx_section = "\n\n# Context Files:\n" + "".join(
            f"\n--- {p} ---\n{c}\n" for p, c in context_parts
        )
        full_prompt = prompt + ctx_section
        if len(full_prompt) > MAX_PROMPT_CHARS:
            # Compress context_files proportionally while keeping base prompt intact
            budget_for_files = MAX_PROMPT_CHARS - len(prompt) - 500
            if budget_for_files > 0:
                per_file = max(budget_for_files // len(context_parts), 500)
                ctx_section = "\n\n# Context Files:\n" + "".join(
                    f"\n--- {p} ---\n{_trim_to_budget(c, per_file)}\n"
                    for p, c in context_parts
                )
                full_prompt = prompt + ctx_section
            else:
                full_prompt = prompt + "\n\n[Context files omitted — prompt too long]"
            ui.log_history(f"Prompt budgeted to {len(full_prompt):,} chars (context compressed)", "warn")
    else:
        full_prompt = prompt
        if len(full_prompt) > MAX_PROMPT_CHARS:
            ui.log_history(f"Prompt too long ({len(full_prompt):,} chars), trimming...", "warn")
            full_prompt = _trim_to_budget(full_prompt, MAX_PROMPT_CHARS)

    ui.log_io(f"\n[{role_name} PROMPT]", "prompt", role_name)
    ui.log_io(prompt[:200] + "...\n[Context Attached]", "prompt", role_name)
    ui.log_io("\n[WAITING FOR CLAUDE...]\n", "prompt", role_name)

    with open(log_file_path, "a") as lf:
        lf.write(f"\n[{role_name} PROMPT]\n=============================\n")
        lf.write(full_prompt)
        lf.write("\n=============================\n[CLAUDE OUTPUT]\n")

    options = ClaudeAgentOptions(
        model=model,
        permission_mode="bypassPermissions",
        cwd=str(PROJECT_ROOT),  # pok/ — workers use relative paths like bots/claude_vN/
        tools=tools,
        disallowed_tools=_BLOCKED_MCP_TOOLS,
        thinking={"type": "adaptive", "display": "summarized"},
    )

    full_text = []
    cost_usd = None
    usage = None

    query_gen = None
    try:
        query_gen = claude_query(prompt=full_prompt, options=options)
        async for message in query_gen:
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text = block.text
                        full_text.append(text)
                        with open(log_file_path, "a") as lf:
                            lf.write(text + "\n")
                        ui.log_io(text, "claude", role_name)
                    elif isinstance(block, ThinkingBlock):
                        ui.log_io(block.thinking or "[thinking...]", "thinking", role_name)
                    elif isinstance(block, ToolUseBlock):
                        ui.log_io(f"\n[tool: {block.name}]", "tool", role_name)
                        ui.emit_tool_call(block.name, block.input, role_name)
                    elif isinstance(block, ToolResultBlock):
                        content = block.content if isinstance(block.content, str) else (
                            json.dumps(block.content, ensure_ascii=False) if block.content is not None else ""
                        )
                        if content:
                            ui.log_io(content[:3000], "tool_result", role_name)
            elif isinstance(message, ResultMessage):
                cost_usd = message.total_cost_usd
                usage = message.usage
    except (CLINotFoundError, ProcessError) as e:
        ui.log_io(f"[ERROR] {e}", "error", role_name)
        if query_gen is not None:
            try:
                await query_gen.aclose()
            except Exception:
                pass
    except asyncio.CancelledError:
        ui.log_io(f"\n[{role_name} CANCELLED]", "error", role_name)
        if query_gen is not None:
            try:
                await query_gen.aclose()
            except Exception:
                pass
        raise

    output = "\n".join(full_text)

    # Auto-retry on API rate limit (529) with exponential backoff
    if _is_rate_limited(output):
        for backoff in [30, 60, 120]:
            ui.log_history(f"API rate limited (529). Retrying in {backoff}s...", "warn")
            await asyncio.sleep(backoff)
            full_text.clear()
            retry_gen = None
            try:
                retry_gen = claude_query(prompt=full_prompt, options=options)
                async for message in retry_gen:
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                text = block.text
                                full_text.append(text)
                                with open(log_file_path, "a") as lf:
                                    lf.write(text + "\n")
                                ui.log_io(text, "claude", role_name)
                            elif isinstance(block, ThinkingBlock):
                                ui.log_io(block.thinking or "[thinking...]", "thinking", role_name)
                            elif isinstance(block, ToolUseBlock):
                                ui.log_io(f"\n[tool: {block.name}]", "tool", role_name)
                                ui.emit_tool_call(block.name, block.input, role_name)
                            elif isinstance(block, ToolResultBlock):
                                content = block.content if isinstance(block.content, str) else (
                                    json.dumps(block.content, ensure_ascii=False) if block.content is not None else ""
                                )
                                if content:
                                    ui.log_io(content[:3000], "tool_result", role_name)
                    elif isinstance(message, ResultMessage):
                        cost_usd = (cost_usd or 0) + (message.total_cost_usd or 0)
                        usage = message.usage
            except (CLINotFoundError, ProcessError) as e:
                ui.log_io(f"[ERROR] {e}", "error", role_name)
                if retry_gen is not None:
                    try:
                        await retry_gen.aclose()
                    except Exception:
                        pass
            except asyncio.CancelledError:
                if retry_gen is not None:
                    try:
                        await retry_gen.aclose()
                    except Exception:
                        pass
                raise

            output = "\n".join(full_text)
            if not _is_rate_limited(output):
                break

    ui.update_cost(role_name, cost_usd, usage)

    return output, cost_usd, usage


def parse_json_output(output):
    match = re.search(r'```json\s*(.*?)\s*```', output, re.DOTALL)
    if match:
        text = match.group(1).strip()
        # Try the full extracted text first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Strip only a single trailing ``` (common LLM artifact) and retry
        if text.endswith('```'):
            try:
                return json.loads(text[:-3].rstrip())
            except json.JSONDecodeError:
                pass
    try:
        return json.loads(output)
    except Exception:
        pass
    return None


# ──────────────────────────────────────────────
# Code Verification
# ──────────────────────────────────────────────

def verify_code(directory):
    errors = []
    for root, _, files in os.walk(directory):
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(root, f)
                proc = subprocess.run([sys.executable, "-m", "py_compile", path], capture_output=True, text=True)
                if proc.returncode != 0:
                    errors.append(proc.stderr.strip())
    return errors


def check_code_size(directory, max_lines_per_file=MAX_LINES_PER_FILE):
    """Check single-file LOC limits (excluding backup files). Returns (total, oversized_files)."""
    oversized_files = []
    total = 0
    for root, _, files in os.walk(directory):
        for f in files:
            if f.endswith(".py") and "backup" not in f:
                path = os.path.join(root, f)
                with open(path) as fh:
                    lines = sum(1 for _ in fh)
                total += lines
                if lines > max_lines_per_file:
                    oversized_files.append((f, lines))
    return total, oversized_files


def run_smoke_test(directory):
    main_path = os.path.join(directory, "main.py")
    if not os.path.exists(main_path):
        return ["main.py not found!"]
    proc = subprocess.run(
        [sys.executable, str(CORE_DIR / "smoke_tester.py"), main_path],
        capture_output=True, text=True
    )
    if proc.returncode != 0:
        return [proc.stderr.strip() or proc.stdout.strip()]
    return []


def run_decision_test_details(directory):
    """Run standard decision scenarios. Returns detailed gate results."""
    main_path = os.path.join(directory, "main.py")
    if not os.path.exists(main_path):
        return {
            "pass_rate": 0.0,
            "passed": 0,
            "total": 0,
            "critical_passed": 0,
            "critical_total": 0,
            "critical_failures": [{"id": "main.py", "details": "main.py not found"}],
            "failures": [{"id": "main.py", "severity": "critical", "details": "main.py not found"}],
            "scenarios": [],
        }
    from decision_tester import run_decision_tests_detail as _run_detail
    return _run_detail(main_path, verbose=False)


def seed_initial_bots(ui):
    """Seed claude_v1 through claude_v6 with bot1 through bot6 if they don't exist."""
    seeded = False
    for i in range(1, 7):
        target_dir = get_bot_dir(i)
        source_dir = REFERENCE_DIR / f"bot{i}"
        if not target_dir.exists() and source_dir.exists():
            ui.log_history(f"Seeding claude_v{i} from reference bot{i}...", "info")
            shutil.copytree(source_dir, target_dir, ignore=_COPY_IGNORE)
            (target_dir / ".completed").touch()
            seeded = True
    return seeded
