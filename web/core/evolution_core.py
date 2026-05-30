"""
Core business logic for the poker bot evolution framework.

This module contains all non-UI logic: bot management, LLM orchestration,
Glicko-2 rating helpers, daemon management, and the main evolution loop.
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
    ThinkingBlock,
    CLINotFoundError,
    ProcessError,
)

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

MAX_ACTIVE_BOTS = 30

# Evaluation & quality thresholds
DAEMON_EVAL_TIMEOUT = 600
MIN_GAMES_FOR_EVAL = 100
MAX_LINES_PER_FILE = 1000
MIN_DECISION_PASS_RATE = 0.7
MIN_CROSSOVER_DECISION_RATE = 0.6
MAX_WORKER_RETRIES = 4
MAX_MASTER_RETRIES = 3
MAX_REVIEWER_RETRIES = 3
MAX_CROSSOVER_RETRIES = 3
MAX_GENESIS_RETRIES = 3
COOLDOWN_THRESHOLD = 3
COOLDOWN_SECONDS = 3600
CONSOLIDATE_EVERY_N_GENS = 3
REPORT_EVERY_N_GENS = 5
MAX_INTRA_GEN_ITERS = 2      # Intra-generation Critic-loop iteration limit
WORKER_TIMEOUT = 1000         # Seconds before a hung worker call is aborted + retried
MAX_PARALLEL_WORKERS = 3      # Hard cap on simultaneous LLM worker calls (Semaphore)

# Prompt size limits — Sonnet supports 200K tokens (~800K chars); leave generous headroom
MAX_PROMPT_CHARS = 700_000

# Lazy-initialised semaphore — created on first use inside the event loop
_WORKER_SEMAPHORE: "asyncio.Semaphore | None" = None


def _get_worker_semaphore() -> "asyncio.Semaphore":
    """Return (creating if needed) the module-level worker concurrency semaphore."""
    global _WORKER_SEMAPHORE
    if _WORKER_SEMAPHORE is None:
        _WORKER_SEMAPHORE = asyncio.Semaphore(MAX_PARALLEL_WORKERS)
    return _WORKER_SEMAPHORE


# ──────────────────────────────────────────────
# Prompt Budget Helpers
# ──────────────────────────────────────────────

def _trim_to_budget(text: str, max_chars: int, tail: bool = False) -> str:
    """Trim text to max_chars. If tail=True, keep the LAST max_chars (most recent content)."""
    if len(text) <= max_chars:
        return text
    note = "\n...[TRIMMED]\n"
    if tail:
        return note + text[-(max_chars - len(note)):]
    return text[:max_chars - len(note)] + note


# ──────────────────────────────────────────────
# Pipeline Checkpoint (Process Recovery)
# ──────────────────────────────────────────────

PIPELINE_STATE_FILE = RESULTS_DIR / "pipeline_state.json"
STAGE_ORDER = ["prepared", "workers_done", "quality_passed", "reviewed", "critic_checked", "verified"]
STAGE_GATE_ALLOWLIST = {
    "prepared": set(),
    "workers_done": set(),
    "quality_passed": {"quality"},
    "reviewed": {"quality", "review"},
    "critic_checked": {"quality", "review", "critic"},
    "verified": {"quality", "review", "critic", "precommit_eval"},
}


def write_pipeline_checkpoint(next_v, source_v, stage, master_plan=None,
                               reviewer_feedback="", generation_attempt=0,
                               gate_results=None):
    """Write pipeline stage checkpoint so a killed process can resume."""
    existing_gate_results = {}
    try:
        if PIPELINE_STATE_FILE.exists():
            with locked_file(PIPELINE_STATE_FILE) as f:
                existing = json.load(f)
            if existing.get("next_v") == next_v and existing.get("source_v") == source_v:
                existing_gate_results = existing.get("gate_results", {}) or {}
    except Exception:
        existing_gate_results = {}
    allowed_gates = STAGE_GATE_ALLOWLIST.get(stage)
    if allowed_gates is not None:
        existing_gate_results = {
            name: data
            for name, data in existing_gate_results.items()
            if name in allowed_gates
        }
    if gate_results:
        existing_gate_results.update(gate_results)

    state = {
        "next_v": next_v, "source_v": source_v, "stage": stage,
        "master_plan": master_plan, "reviewer_feedback": reviewer_feedback,
        "generation_attempt": generation_attempt,
        "gate_results": existing_gate_results,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with locked_file(PIPELINE_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


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


def _stage_done(resume_stage, this_stage):
    """True if resume_stage indicates this_stage has already been completed."""
    if not resume_stage:
        return False
    try:
        return STAGE_ORDER.index(resume_stage) >= STAGE_ORDER.index(this_stage)
    except ValueError:
        return False


@contextmanager
def locked_file(path, mode='r', lock_type=None, encoding=None):
    """Context manager for file operations with fcntl locking."""
    if lock_type is None:
        lock_type = fcntl.LOCK_EX if ('w' in mode or 'a' in mode or '+' in mode) else fcntl.LOCK_SH
    open_kwargs = {}
    if encoding is not None:
        open_kwargs["encoding"] = encoding
    with open(path, mode, **open_kwargs) as f:
        fcntl.flock(f, lock_type)
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

# Add workspace to sys.path for glicko2 import
from glicko2 import Glicko2Player, update_rating_period
from experience_pool import trim_experience_pool

# Global daemon process handle
daemon_proc = None
_daemon_lock = threading.Lock()
_atexit_registered = False


# ──────────────────────────────────────────────
# UI Interface
# ──────────────────────────────────────────────

class BaseUI:
    def log_history(self, msg, status="info"): pass
    def set_status(self, msg, is_working=False): pass
    def log_io(self, msg, stream_type="default"): pass
    def clear_io(self): pass
    def update_eval_table(self, ratings, active_bots): pass
    def update_daemon_status(self, stats, ratings): pass
    def set_header(self, msg): pass
    def update_cost(self, role, cost_usd, usage): pass
    def update_metrics(self, metrics): pass
    def emit_tool_call(self, tool_name: str, args: dict): pass


class TextUI(BaseUI):
    def log_history(self, msg, status="info"):
        print(f"[HISTORY] {msg}")
    def set_status(self, msg, is_working=False):
        print(f"[STATUS] {msg}")
    def log_io(self, msg, stream_type="default"):
        pass
    def clear_io(self):
        pass
    def update_cost(self, role, cost_usd, usage):
        if cost_usd is not None:
            print(f"[COST] {role}: ${cost_usd:.4f}")
    def update_metrics(self, metrics):
        m = metrics
        total_s = m.get("total_time_s", 0)
        avg_s = m.get("avg_gen_time_s", 0)
        trend = m.get("rating_trend", 0)
        print(f"[METRICS] Gen v{m.get('current_v','?')}→v{m.get('next_v','?')} | "
              f"Time: {total_s//60}m{total_s%60}s | Avg: {int(avg_s)//60}m{int(avg_s)%60}s | "
              f"Rate: {m.get('success_rate',0):.0%} | Trend: {trend:+.0f}")


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def get_bot_dir(version):
    return BOTS_DIR / f"claude_v{version}"


def get_logs_dir(version):
    d = RESULTS_DIR / f"v{version}" / "logs"
    os.makedirs(d, exist_ok=True)
    return d


def get_active_bots():
    bots = []
    if BOTS_DIR.exists():
        for d in os.listdir(BOTS_DIR):
            if d.startswith("claude_v") and os.path.isdir(BOTS_DIR / d):
                if (BOTS_DIR / d / ".completed").exists():
                    bots.append(d)
    return sorted(bots, key=lambda x: int(x.split("_v")[1]))


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


async def wait_for_daemon_eval(bot_name, timeout=DAEMON_EVAL_TIMEOUT, min_games=MIN_GAMES_FOR_EVAL):
    """Wait for daemon to evaluate a new bot (async, non-blocking).

    Requires sufficient games played. Uses mtime caching to avoid redundant disk reads.
    """
    start = time.time()
    cached_bot_stats = None
    bot_stats_mtime = 0

    while time.time() - start < timeout:
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
        await asyncio.sleep(5)
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
        daemon_script = str(CORE_DIR / "elo_daemon.py")
        cmd = [sys.executable, daemon_script, "--workers", str(workers), "--pairs", str(pairs)]
        daemon_proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            start_new_session=True,  # Independent process group for clean killpg
        )
    # Drain daemon stdout to prevent pipe buffer deadlock
    threading.Thread(target=_drain_stdout, args=(daemon_proc,), daemon=True).start()
    if not _atexit_registered:
        atexit.register(stop_daemon)
        _atexit_registered = True
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
                daemon_proc.wait(timeout=3)
        daemon_proc = None


def daemon_monitor_thread(ui, stop_event):
    """Background thread that periodically reads daemon stats and updates UI."""
    while not stop_event.is_set():
        try:
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


EVOLUTION_BRANCH = "main"


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
    _git("stash", check=False)
    _git("checkout", EVOLUTION_BRANCH, check=False)
    _git("stash", "pop", check=False)


def git_ensure_clean():
    """Stage + commit all tracked files as checkpoint on the evolution branch."""
    _git_ensure_main_branch()
    _git("add", "-A")
    status = _git("status", "--porcelain")
    if status:
        try:
            _git("commit", "-m", "checkpoint: pre-evolution housekeeping")
        except RuntimeError:
            # Might fail if no git user configured; skip silently
            pass
    return _git("rev-parse", "HEAD")


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
    # Delete existing tag if any (interrupted run), then create fresh
    _git("tag", "-d", tag, check=False)
    _git("tag", tag, "-m", f"Bot v{version}: {strategy_tag}")




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


def git_get_ancestors(version):
    """Walk parent chain via git."""
    ancestors = []
    current = version
    seen = set()
    while current and current not in seen:
        seen.add(current)
        parent_name = git_get_parent(current)
        if parent_name:
            parent_v = int(parent_name.split("_v")[1])
            ancestors.append(parent_name)
            current = parent_v
        else:
            break
    return ancestors


def git_get_stagnation_count(bot_name, ratings):
    """Count consecutive non-improving ancestors."""
    version = int(bot_name.split("_v")[1])
    count = 0
    current = version

    while current:
        parent_name = git_get_parent(current)
        if not parent_name:
            break
        parent_v = int(parent_name.split("_v")[1])
        current_rating = ratings.get(f"claude_v{current}", Glicko2Player()).r
        parent_rating = ratings.get(parent_name, Glicko2Player()).r
        if current_rating <= parent_rating:
            count += 1
            current = parent_v
        else:
            break
    return count



# ──────────────────────────────────────────────
# LLM & Code Tools
# ──────────────────────────────────────────────

# MCP servers to block for sub-agents (keep zai-mcp-server for vision, block the rest)
_BLOCKED_MCP_TOOLS = [
    "mcp__web-reader__webReader",
    "mcp__web-search-prime__web_search_prime",
    "mcp__zread__get_repo_structure",
    "mcp__zread__read_file",
    "mcp__zread__search_doc",
]

async def run_claude_query(prompt, context_files, ui, role_name, log_file_path, is_text_ui, model="sonnet", tools=None):
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

    ui.log_io(f"\n[{role_name} PROMPT]", "prompt")
    ui.log_io(prompt[:200] + "...\n[Context Attached]", "prompt")
    ui.log_io("\n[WAITING FOR CLAUDE...]\n", "prompt")

    with open(log_file_path, "a") as lf:
        lf.write(f"\n[{role_name} PROMPT]\n=============================\n")
        lf.write(full_prompt)
        lf.write("\n=============================\n[CLAUDE OUTPUT]\n")

    if is_text_ui:
        print(f"\n[{role_name} STARTED]")

    options = ClaudeAgentOptions(
        model=model,
        permission_mode="bypassPermissions",
        cwd=str(PROJECT_ROOT),  # pok/ — workers use relative paths like bots/claude_vN/
        tools=tools,
        disallowed_tools=_BLOCKED_MCP_TOOLS,
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
                        if not is_text_ui:
                            ui.log_io(text, "claude")
                        else:
                            print(text)
                    elif isinstance(block, ThinkingBlock):
                        ui.log_io("[thinking...]", "thinking")
                    elif isinstance(block, ToolUseBlock):
                        ui.log_io(f"[tool: {block.name}]", "tool")
            elif isinstance(message, ResultMessage):
                cost_usd = message.total_cost_usd
                usage = message.usage
    except (CLINotFoundError, ProcessError) as e:
        ui.log_io(f"[ERROR] {e}", "error")
    except asyncio.CancelledError:
        ui.log_io(f"\n[{role_name} CANCELLED]", "error")
        if query_gen is not None:
            await query_gen.aclose()
        raise

    ui.update_cost(role_name, cost_usd, usage)

    output = "\n".join(full_text)

    # Auto-retry on API rate limit (529) with exponential backoff
    if "529" in output or "该模型当前访问量过大" in output or "rate limit" in output.lower():
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
                                if not is_text_ui:
                                    ui.log_io(text, "claude")
                                else:
                                    print(text)
                            elif isinstance(block, ThinkingBlock):
                                ui.log_io("[thinking...]", "thinking")
                            elif isinstance(block, ToolUseBlock):
                                ui.log_io(f"[tool: {block.name}]", "tool")
                    elif isinstance(message, ResultMessage):
                        cost_usd = message.total_cost_usd
                        usage = message.usage
            except (CLINotFoundError, ProcessError) as e:
                ui.log_io(f"[ERROR] {e}", "error")
            except asyncio.CancelledError:
                if retry_gen is not None:
                    await retry_gen.aclose()
                raise

            ui.update_cost(role_name, cost_usd, usage)
            output = "\n".join(full_text)
            if "529" not in output and "该模型当前访问量过大" not in output:
                break

    return output, cost_usd, usage


def parse_json_output(output):
    match = re.search(r'```json\s*(.*)\s*```', output, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except:
            text = match.group(1)
            while '```' in text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    text = text.rsplit('```', 1)[0]
            try:
                return json.loads(text)
            except:
                pass
    try:
        return json.loads(output)
    except:
        pass
    return None


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


def run_decision_tests(directory):
    """Run standard decision scenarios. Returns pass rate (0.0 - 1.0)."""
    return run_decision_test_details(directory)["pass_rate"]


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


def get_reference_context():
    context = []
    if REFERENCE_DIR.exists():
        for b in range(1, 7):
            ref_path = REFERENCE_DIR / f"bot{b}"
            if ref_path.exists():
                for f in os.listdir(ref_path):
                    if f.endswith(".py"):
                        context.append(str(ref_path / f))
    return context


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


# ──────────────────────────────────────────────
# Master Analysis
# ──────────────────────────────────────────────

async def _run_master_analysis(source_v, next_v, stagnation_info, ui, is_text_ui,
                               match_analysis="", performance_verification=""):
    """Run Master analysis — can run concurrently with daemon evaluation."""
    master_prompt = (PROMPTS_DIR / "master_prompt.md").read_text()
    # Apply section budgets to avoid experience_pool crowding out match_analysis
    match_analysis_trimmed = _trim_to_budget(match_analysis, 10_000, tail=True)
    perf_trimmed = _trim_to_budget(
        performance_verification if performance_verification
        else "No performance verification data available.",
        4_000
    )
    master_prompt = substitute_template(master_prompt, {
        "stagnation_info": stagnation_info,
        "match_analysis": match_analysis_trimmed,
        "performance_verification": perf_trimmed,
    })
    master_ctx = (
        f"Current evolution: v{source_v} → v{next_v}\n"
        f"Bot directory: bots/claude_v{source_v}/\n"
        f"Ratings file: web/core/results/glicko_ratings.json\n"
        f"Rating history: web/core/results/rating_history.jsonl\n"
        f"Head-to-Head data: web/core/results/head_to_head.json\n"
        f"Bot stats: web/core/results/bot_stats.json\n"
        f"Experience pool: web/core/experience_pool.md  ← READ THIS, not evolution_workspace/experience_pool.md\n"
    )
    master_log_file = get_logs_dir(next_v) / "master_io.txt"

    for attempt in range(MAX_MASTER_RETRIES):
        ui.clear_io()
        output, _, _ = await run_claude_query(
            master_prompt + "\n" + master_ctx, [], ui,
            f"MASTER (Try {attempt+1})", master_log_file, is_text_ui,
            tools=["Bash", "Read"],
        )
        data = parse_json_output(output)
        if data and "tasks" in data:
            ui.log_history("Master analysis complete.", "success")
            return data
        ui.log_history("Master output malformed JSON. Retrying...", "warn")
        await asyncio.sleep(2)

    ui.log_history(f"Master failed to plan after {MAX_MASTER_RETRIES} retries.", "error")
    return None


async def _consolidate_experience_pool(ui, is_text_ui):
    """Use LLM to deduplicate and consolidate the experience pool.

    Reads the current experience_pool.md, asks LLM to merge redundant entries,
    and writes back a consolidated version. Runs every 3 generations.

    Strategy: ask LLM to output the consolidated text directly (not edit in-place),
    then write it back here as a guaranteed fallback. The LLM's text output is the
    source of truth — no dependency on the agent using Edit tool.
    """
    if not EXPERIENCE_FILE.exists():
        return

    with locked_file(EXPERIENCE_FILE, "r") as ef:
        content = ef.read()
    if not content or len(content.split("\n")) < 20:
        return  # Too short to bother consolidating

    consolidate_prompt = (
        "You are an Experience Pool Consolidator. Your job is to clean up the experience pool file.\n\n"
        "RULES:\n"
        "1. Read the current experience pool content provided below.\n"
        "2. Merge duplicate or near-duplicate lessons into single, concise bullet points.\n"
        "3. Keep the most recent/relevant version of each lesson.\n"
        "4. Remove entries superseded by newer findings.\n"
        "5. Keep the total output under 70 lines.\n"
        "6. Output ONLY the consolidated markdown — no explanation, no code fences.\n\n"
        "CRITICAL — Output MUST use exactly these category headers (in this order):\n"
        "## OPPONENT_MODELING\n"
        "## POSTFLOP_STRATEGY\n"
        "## BLUFF_CALIBRATION\n"
        "## PARAMETER_TUNING\n"
        "## GENERAL\n"
        "## RECENT_LESSONS\n\n"
        "Sort each lesson into the most relevant category.\n"
        "RECENT_LESSONS should contain only lessons from the last 3 generations.\n\n"
        "LOCAL OPTIMA FLAG: If the same type of lesson appears for 3+ consecutive "
        "generations (e.g. 3 gens of constant-tuning in the same direction with no gain), "
        "append ' [POSSIBLY EXHAUSTED]' to that bullet so Master avoids repeating it.\n\n"
        "## Current experience_pool.md content:\n\n"
        f"{content}\n\n"
        "## Output the consolidated version now (plain markdown, no fences):"
    )
    log_file = get_logs_dir(0) / "experience_consolidation_io.txt"

    try:
        ui.clear_io()
        output, _, _ = await run_claude_query(
            consolidate_prompt, [], ui,
            "EXPERIENCE CONSOLIDATOR", log_file, is_text_ui,
        )
        consolidated = output.strip() if output else ""
        # Strip accidental code fences if LLM added them
        if consolidated.startswith("```"):
            lines = consolidated.split("\n")
            consolidated = "\n".join(
                l for l in lines if not l.strip().startswith("```")
            ).strip()

        if consolidated and len(consolidated) > 50:
            with locked_file(EXPERIENCE_FILE, "w") as ef:
                ef.write(consolidated + "\n")
            ui.log_history("Experience pool consolidated and written back.", "success")
        else:
            ui.log_history("Experience pool consolidation produced no output — skipping write.", "warn")
    except Exception as e:
        ui.log_history(f"Experience pool consolidation failed: {e}", "warn")


async def _generate_evolution_report(current_v, total_gens, ratings, ui, is_text_ui):
    """Generate a brief evolution status report every 5 generations."""
    sorted_bots = sorted(
        [(b, ratings.get(b, Glicko2Player())) for b in ratings],
        key=lambda x: x[1].r, reverse=True,
    )[:5]

    prompt = "Summarize the current poker bot evolution status in 3-4 bullet points.\n"
    prompt += f"Total generations completed: {total_gens}, latest: v{current_v}\nTop 5:\n"
    for b, p in sorted_bots:
        prompt += f"  {b}: r={p.r:.0f} rd={p.rd:.0f}\n"
    prompt += "Focus on: strategy trends, population health, and next priorities. Keep it concise."

    log_file = get_logs_dir(current_v) / "evolution_report.txt"
    output, _, _ = await run_claude_query(
        prompt, [], ui, "EVOLUTION REPORTER", log_file, is_text_ui,
    )
    if output:
        for line in output.strip().split("\n"):
            if line.strip():
                ui.log_history(line.strip(), "info")


async def _analyze_stagnation(source_v, active_bots, ratings, ui, is_text_ui):
    """Use LLM to analyze rating trends and determine if stagnation is real.

    Returns a dict with: is_stagnant, confidence, recommendation, branch_from, reason.
    Returns None on failure.
    """
    # Build compact context from rating history
    history_file = RESULTS_DIR / "rating_history.jsonl"
    history_ctx = ""
    if history_file.exists():
        with open(history_file) as f:
            lines = f.readlines()
        for line in lines[-10:]:
            try:
                snap = json.loads(line.strip())
                top = max(p["r"] for p in snap["ratings"].values())
                history_ctx += f"  Period {snap['period']}: top_r={top:.0f}\n"
            except (json.JSONDecodeError, KeyError):
                continue

    sorted_bots = sorted(active_bots, key=lambda b: ratings.get(b, Glicko2Player()).r, reverse=True)[:5]

    prompt = (
        "You are a rating trend analyst for a poker bot evolution system.\n"
        "Analyze whether the evolution is truly stagnating or if rating changes are just Glicko variance.\n\n"
        f"Current bot: claude_v{source_v}\n"
        f"Top 5 bots by rating:\n"
    )
    for b in sorted_bots:
        p = ratings.get(b, Glicko2Player())
        prompt += f"  {b}: r={p.r:.0f} rd={p.rd:.0f}\n"
    prompt += f"\nRating history (last 10 periods):\n{history_ctx}\n"
    prompt += (
        "Is this real stagnation or Glicko variance? Answer in JSON only:\n"
        '```json\n'
        '{"is_stagnant": true/false, "confidence": "high/medium/low", '
        '"recommendation": "continue|branch|crossover", '
        '"branch_from": "claude_vN" or null, '
        '"reason": "brief explanation"}\n'
        '```'
    )

    log_file = get_logs_dir(source_v) / "stagnation_analysis.txt"
    output, _, _ = await run_claude_query(
        prompt, [], ui, "STAGNATION ANALYST", log_file, is_text_ui,
    )
    return parse_json_output(output)


def _num_public_cards_to_street(n):
    """Map community-card count to street name."""
    return {0: "preflop", 3: "flop", 4: "turn", 5: "river"}.get(n, f"street_{n}")


def extract_street_patterns(games, bot_idx):
    """Extract per-street action frequencies from a list of game dicts.

    Returns a dict mapping street name → action counts, plus a compact text summary.
    Used by summarize_replay_for_analysis() to detect street-specific weaknesses.
    """
    from collections import defaultdict
    streets = {s: defaultdict(int) for s in ("preflop", "flop", "turn", "river")}

    for g in games:
        for log in g.get("logs", []):
            out = log.get("output")
            if not out or not isinstance(out, dict):
                continue
            display = out.get("display")
            if not display or not isinstance(display, dict):
                continue
            action_info = display.get("last_action")
            if not action_info or not isinstance(action_info, dict):
                continue
            if action_info.get("player_id") != bot_idx:
                continue

            # Determine street from number of community cards present BEFORE this action
            n_community = len(display.get("public_cards", []))
            street = _num_public_cards_to_street(n_community)
            if street not in streets:
                continue

            act_val = action_info.get("action", 0)
            if act_val == -1:
                streets[street]["fold"] += 1
            elif act_val == -2:
                streets[street]["allin"] += 1
            elif act_val > 0:
                streets[street]["raise"] += 1
                # Track raise size relative to pot (pot available from display)
                pot = display.get("pot", 0)
                if pot > 0:
                    streets[street]["raise_size_sum"] += act_val
                    streets[street]["raise_size_pot_sum"] += act_val / pot
                    streets[street]["raise_size_count"] += 1
            else:
                streets[street]["call"] += 1

    # Build compact text lines
    lines = []
    for street in ("preflop", "flop", "turn", "river"):
        s = streets[street]
        total = s["fold"] + s["raise"] + s["call"] + s["allin"]
        if total == 0:
            continue
        parts = [
            f"fold={s['fold']*100//total}%",
            f"raise={s['raise']*100//total}%",
            f"call={s['call']*100//total}%",
        ]
        if s["allin"] > 0:
            parts.append(f"allin={s['allin']*100//total}%")
        if s.get("raise_size_count", 0) > 0:
            avg_ratio = s["raise_size_pot_sum"] / s["raise_size_count"]
            parts.append(f"avg_raise={avg_ratio:.1f}x_pot")
        lines.append(f"  {street.capitalize()}: {', '.join(parts)}")

    return "\n".join(lines) if lines else ""


def summarize_replay_for_analysis(replay_data, bot_name):
    """Extract structured statistics from replay JSON for LLM analysis.

    Compresses ~253 game logs into a compact ~500 token summary covering
    win rates, chip distribution, fold frequency, key action patterns,
    and per-street behaviour breakdown.
    """
    bot_idx = None
    opp_idx = None
    if replay_data.get("bot0") == bot_name:
        bot_idx, opp_idx = 0, 1
    elif replay_data.get("bot1") == bot_name:
        bot_idx, opp_idx = 1, 0
    if bot_idx is None:
        return ""

    games = replay_data.get("games", [])
    total_games = len(games)
    if total_games == 0:
        return ""

    wins = sum(1 for g in games if g.get("winner") == bot_idx)
    chip_deltas = [g.get(f"bot{bot_idx}_chips", 0.0) for g in games]

    lines = []
    lines.append(f"Match: {replay_data['bot0']} vs {replay_data['bot1']}, "
                 f"Result: {wins}W/{total_games - wins}L out of {total_games} games")
    lines.append(f"Chip delta: avg={sum(chip_deltas)/len(chip_deltas):.0f}, "
                 f"best={max(chip_deltas):.0f}, worst={min(chip_deltas):.0f}")

    # Per-game action analysis
    fold_count = 0
    raise_count = 0
    call_count = 0
    allin_count = 0
    big_pot_losses = []  # games where bot lost big pots

    for g in games:
        game_chip = g.get(f"bot{bot_idx}_chips", 0.0)
        logs = g.get("logs", [])

        for log in logs:
            out = log.get("output")
            if not out or not isinstance(out, dict):
                continue

            # Count actions from request content (bot's own actions)
            content = out.get("content", {})
            if isinstance(content, dict):
                player_data = content.get(str(bot_idx), {})
                if isinstance(player_data, dict):
                    history = player_data.get("history", [])
                    # Last entry in history is the most recent action
                    # But this is request data, action comes from response
                    continue

            # Count from display data
            display = out.get("display")
            if display and isinstance(display, dict):
                action = display.get("last_action")
                if action and isinstance(action, dict):
                    pid = action.get("player_id")
                    if pid == bot_idx:
                        act_val = action.get("action", 0)
                        if act_val == -1:
                            fold_count += 1
                        elif act_val == -2:
                            allin_count += 1
                        elif act_val > 0:
                            raise_count += 1
                        else:
                            call_count += 1

        if game_chip < -5000:
            big_pot_losses.append((g.get("game", "?"), game_chip))

    total_actions = fold_count + raise_count + call_count + allin_count
    if total_actions > 0:
        lines.append(f"Actions: fold={fold_count}({fold_count*100//total_actions}%), "
                     f"call={call_count}({call_count*100//total_actions}%), "
                     f"raise={raise_count}({raise_count*100//total_actions}%), "
                     f"allin={allin_count}({allin_count*100//total_actions}%)")

    if big_pot_losses:
        lines.append(f"Big losses (>-5000): {len(big_pot_losses)} games")
        for gid, delta in big_pot_losses[:3]:
            lines.append(f"  Game {gid}: {delta:.0f} chips")

    # Per-street action breakdown (StratFormer-style opponent modelling insight)
    street_summary = extract_street_patterns(games, bot_idx)
    if street_summary:
        lines.append("Per-street actions (bot):")
        lines.append(street_summary)

    return "\n".join(lines)


REPLAY_DIR = RESULTS_DIR / "match_replay"
MATCH_HISTORY_FILE = RESULTS_DIR / "match_history.jsonl"


def _record_worker_failure(gen, worker_id, role, error):
    """Append a worker failure record to the JSONL file."""
    entry = {"gen": gen, "worker_id": worker_id, "role": role, "error": error}
    with locked_file(WORKER_FAILURES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _load_recent_failures(n=3):
    """Load the n most recent worker failure records."""
    if not WORKER_FAILURES_FILE.exists():
        return []
    entries = []
    with locked_file(WORKER_FAILURES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries[-n:]


async def _analyze_recent_matches(source_v, ui, is_text_ui, max_matches=8):
    """Use LLM to analyze recent replay data for the current bot.

    Collects both recent losses and close wins (margin < 3 games) to give
    the Master a balanced view of weaknesses and what's working.

    Returns a match analysis string to inject into Master's context, or ""
    if no replay data is available.
    """
    bot_name = f"claude_v{source_v}"

    if not MATCH_HISTORY_FILE.exists():
        return ""

    recent_losses = []
    close_wins = []

    with locked_file(MATCH_HISTORY_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            b0, b1 = entry.get("bot0"), entry.get("bot1")
            w0, w1 = entry.get("bot0_wins", 0), entry.get("bot1_wins", 0)

            if b0 == bot_name:
                bot_wins, opp_wins = w0, w1
            elif b1 == bot_name:
                bot_wins, opp_wins = w1, w0
            else:
                continue

            if opp_wins > bot_wins:
                recent_losses.append(entry)
            elif bot_wins > opp_wins and (bot_wins - opp_wins) <= 2:
                # Close win (margin ≤ 2 games) — reveals near-miss vulnerabilities
                close_wins.append(entry)

    if not recent_losses and not close_wins:
        return ""

    recent_losses = recent_losses[-max_matches:]
    close_wins = close_wins[-(max_matches // 2):]

    def _load_summaries(entries, label):
        result = []
        for entry in entries:
            replay_path = REPLAY_DIR / entry["id"]
            if not replay_path.exists():
                continue
            try:
                with open(replay_path, "r") as rf:
                    replay_data = json.load(rf)
                summary = summarize_replay_for_analysis(replay_data, bot_name)
                if summary:
                    result.append(f"[{label}] {summary}")
            except (json.JSONDecodeError, OSError):
                continue
        return result

    summaries = _load_summaries(recent_losses, "LOSS") + _load_summaries(close_wins, "CLOSE WIN")

    if not summaries:
        return ""

    # Call LLM for analysis
    match_analyst_prompt = (
        "You are a Poker Hand Analyst specializing in Texas Hold'em bot strategy.\n"
        "Analyze the following match replay summaries (losses and close wins) for weaknesses and patterns.\n\n"
    )
    match_analyst_prompt += "## Recent Match Summaries (LOSS = bot lost, CLOSE WIN = bot won by ≤2 games)\n\n"
    for s in summaries:
        match_analyst_prompt += s + "\n\n"
    match_analyst_prompt += (
        "Based on the data above, identify:\n"
        "1. Key weaknesses (e.g., folding too much, not raising enough, poor all-in timing)\n"
        "2. Street-specific weaknesses from the Per-street actions data:\n"
        "   - River fold rate ≥40% → scared-money, consider expanding river calling range\n"
        "   - Flop raise rate ≤10% → too passive postflop, giving free cards\n"
        "   - Preflop raise rate ≤15% → limping too much, losing positional advantage\n"
        "   - avg_raise < 0.5x pot on river with big pot → underbetting strong hands\n"
        "3. Any detectable patterns (e.g., weak out-of-position, poor against aggressive opponents)\n"
        "4. What seems to be working (from close wins, if any)\n"
        "5. A concrete recommendation for improvement (be specific: which street, what change)\n\n"
        "Output ONLY a JSON block:\n"
        "```json\n"
        '{"weaknesses": ["..."], "street_weaknesses": {"river": "...", "flop": "..."}, '
        '"patterns": "...", "working": "...", "recommendation": "..."}\n'
        "```\n"
        "Keep it concise — 2-3 weaknesses, specific street observations, 1 recommendation."
    )

    log_file = get_logs_dir(source_v) / "match_analyst_io.txt"
    try:
        output, _, _ = await run_claude_query(
            match_analyst_prompt, [], ui,
            "MATCH ANALYST", log_file, is_text_ui,
        )
        return output or ""
    except Exception:
        return ""


async def _run_critic(next_v, source_v, master_plan_str, ui, is_text_ui):
    """Poker Strategy Critic — independently scores the strategic value of worker changes.

    Separate from the Reviewer (which checks code correctness and role boundaries).
    The Critic evaluates whether the diff will actually improve poker win rate.

    Returns a dict: {score, approved, strategic_assessment, feedback, local_optima_warning}.
    Returns a safe default on failure so the pipeline can always proceed.
    """
    critic_prompt_path = PROMPTS_DIR / "critic_prompt.md"
    if not critic_prompt_path.exists():
        return {"score": 7, "approved": True, "feedback": "Critic prompt not found — defaulting to approved."}

    critic_prompt = critic_prompt_path.read_text()
    critic_prompt = substitute_template(critic_prompt, {
        "master_plan": master_plan_str,
        "version": str(next_v),
        "parent_version": str(source_v),
    })

    log_file = get_logs_dir(next_v) / "critic_io.txt"
    try:
        output, _, _ = await run_claude_query(
            critic_prompt, [], ui, "STRATEGY CRITIC", log_file, is_text_ui,
            tools=["Bash", "Read"],
        )
        data = parse_json_output(output)
        if data and "score" in data:
            # Normalise: score >= 6 → approved
            data.setdefault("approved", data["score"] >= 6)
            return data
    except Exception as e:
        ui.log_history(f"Critic error: {e}. Defaulting to approved.", "warn")

    return {"score": 6, "approved": True, "feedback": "Critic unavailable — proceeding.", "local_optima_warning": False}


async def _run_performance_verification(source_v, ratings, ui, is_text_ui):
    """SATLUTION-style LLM performance verification.

    Synthesises rating history + win-rate trends into a structured JSON insight
    that Master uses to prioritise improvements and avoid local optima.

    Returns a JSON-formatted string (to be injected into master prompt).
    Returns "" on failure so master prompt degrades gracefully.
    """
    # ── Build rating history for last 10 periods ──
    history_file = RESULTS_DIR / "rating_history.jsonl"
    gen_trend_lines = []
    if history_file.exists():
        try:
            with locked_file(history_file, "r") as hf:
                raw_lines = hf.readlines()
            for line in raw_lines[-10:]:
                try:
                    snap = json.loads(line.strip())
                    bots_in_snap = snap.get("ratings", {})
                    top_r = max((v.get("r", 1500) for v in bots_in_snap.values()), default=1500)
                    gen_trend_lines.append(f"  Period {snap.get('period','?')}: top_r={top_r:.0f}")
                except (json.JSONDecodeError, KeyError):
                    continue
        except Exception:
            pass

    # ── Win-rate summary for source_v (last 30 matches) ──
    bot_name = f"claude_v{source_v}"
    win_rate_lines = []
    if MATCH_HISTORY_FILE.exists():
        try:
            wins, losses = 0, 0
            with locked_file(MATCH_HISTORY_FILE, "r") as mf:
                all_lines = mf.readlines()
            for line in all_lines[-100:]:
                try:
                    entry = json.loads(line.strip())
                    b0, b1 = entry.get("bot0"), entry.get("bot1")
                    w0, w1 = entry.get("bot0_wins", 0), entry.get("bot1_wins", 0)
                    if b0 == bot_name:
                        wins += w0; losses += w1
                    elif b1 == bot_name:
                        wins += w1; losses += w0
                except (json.JSONDecodeError, KeyError):
                    continue
            total = wins + losses
            if total > 0:
                win_rate_lines.append(f"  {bot_name} recent: {wins}W / {losses}L ({wins*100//total}% win rate)")
        except Exception:
            pass

    # ── Top-5 active bots for context ──
    active_bots = get_active_bots()
    sorted_bots = sorted(
        [(b, ratings.get(b, Glicko2Player())) for b in active_bots],
        key=lambda x: x[1].r, reverse=True
    )[:5]
    ratings_lines = [f"  {b}: r={p.r:.0f} rd={p.rd:.0f}" for b, p in sorted_bots]

    # ── Head-to-Head data ──
    h2h_lines = []
    if H2H_FILE.exists():
        try:
            with locked_file(H2H_FILE, "r") as hf:
                h2h_data = json.load(hf)
            for k, v in h2h_data.items():
                parts = k.split(" vs ")
                if len(parts) != 2:
                    continue
                a_name, b_name = parts
                if bot_name not in (a_name, b_name):
                    continue
                opponent = b_name if bot_name == a_name else a_name
                g = v.get("games", 0)
                if g == 0:
                    continue
                # Figure out which side our bot is
                if bot_name == a_name:
                    bot_w = v.get("a_wins", 0)
                else:
                    bot_w = v.get("b_wins", 0)
                opp_w = g - bot_w - v.get("draws", 0)
                wr = bot_w / g
                tag = ""
                if wr < 0.40:
                    tag = " ← WEAKNESS"
                elif wr > 0.60:
                    tag = " ← STRENGTH"
                h2h_lines.append((wr, f"  vs {opponent}: {bot_w}W-{opp_w}L ({wr:.0%}){tag}"))
            h2h_lines.sort(key=lambda x: x[0])
        except Exception:
            pass

    # ── Bot stats (overall win rate) ──
    bot_stats_line = ""
    if BOT_STATS_FILE.exists():
        try:
            with locked_file(BOT_STATS_FILE, "r") as bsf:
                bs_data = json.load(bsf)
            bs = bs_data.get(bot_name, {})
            g = bs.get("games", 0)
            wr = bs.get("win_rate", 0.0)
            if g > 0:
                bot_stats_line = f"  {bot_name}: {wr:.0%} overall ({g} games)"
        except Exception:
            pass

    # ── Build prompt ──
    prompt = (
        "You are a Performance Verification Analyst for a self-evolving poker bot system.\n"
        "Your job: synthesise the quantitative data below into actionable LLM-readable insight.\n\n"
        f"Current bot under analysis: {bot_name}\n\n"
        "## Rating History (last 10 periods, top rating)\n"
        + ("\n".join(gen_trend_lines) if gen_trend_lines else "  No history available") + "\n\n"
        "## Overall Win Rate\n"
        + (bot_stats_line if bot_stats_line else "  No stats available") + "\n\n"
        "## Head-to-Head Results (per-opponent)\n"
        + ("\n".join(l for _, l in h2h_lines) if h2h_lines else "  No H2H data available") + "\n\n"
        "## Top Active Bots\n"
        + "\n".join(ratings_lines) + "\n\n"
        "Produce a JSON block answering:\n"
        "```json\n"
        '{"trend": "improving|stagnant|declining",\n'
        ' "verified_improvements": ["list of things that actually helped recent gens"],\n'
        ' "persistent_weaknesses": ["list of recurring problems not yet fixed"],\n'
        ' "diversity_needed": true|false,\n'
        ' "diversity_reason": "why diversity is needed (or null)",\n'
        ' "suggestion": "one concrete high-priority suggestion for next gen"}\n'
        "```\n"
        "Set `diversity_needed: true` if: trend is stagnant/declining for 2+ gens, "
        "OR the last 2 gens applied the same type of change. Be direct and concise."
    )

    log_file = get_logs_dir(source_v) / "performance_verification_io.txt"
    try:
        output, _, _ = await run_claude_query(
            prompt, [], ui, "PERFORMANCE ANALYST", log_file, is_text_ui,
        )
        data = parse_json_output(output)
        if data:
            return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        ui.log_history(f"Performance verification failed: {e}", "warn")

    return ""


async def _run_crossover(parent_a_v, parent_b_v, target_v, ui, is_text_ui):
    """Run crossover between two elite bots to create a new child bot."""
    crossover_prompt = (PROMPTS_DIR / "crossover_prompt.md").read_text()
    crossover_prompt = substitute_template(crossover_prompt, {
        "parent_a_version": str(parent_a_v),
        "parent_b_version": str(parent_b_v),
        "version": str(target_v),
    })

    target_dir = get_bot_dir(target_v)
    log_file = get_logs_dir(target_v) / "crossover_io.txt"

    for attempt in range(MAX_CROSSOVER_RETRIES):
        ui.clear_io()
        ui.set_status(f"Crossover v{parent_a_v}×v{parent_b_v}→v{target_v} (Try {attempt+1})", is_working=True)
        await run_claude_query(
            crossover_prompt, [], ui,
            f"CROSSOVER v{parent_a_v}×v{parent_b_v}→v{target_v}",
            log_file, is_text_ui,
            tools=["Bash", "Read", "Edit"],
        )

        compile_errors = verify_code(target_dir)
        if compile_errors:
            ui.log_history("Crossover compile error, retrying...", "warn")
            continue

        smoke_errors = run_smoke_test(target_dir)
        if smoke_errors:
            ui.log_history("Crossover smoke test failed, retrying...", "warn")
            continue

        return True

    return False


# ──────────────────────────────────────────────
# Worker Execution
# ──────────────────────────────────────────────

async def _run_single_worker(task, idx, worker_template, next_dir, next_v,
                              context_files, ui, is_text_ui, reviewer_feedback):
    """Run a single worker task with retries. Returns True on success."""
    w_id = task.get("worker_id", idx + 1)
    role = task.get("role", f"Expert Coder {w_id}")
    base_worker_prompt = task.get("worker_prompt", task.get("instruction", ""))

    if reviewer_feedback:
        base_worker_prompt = f"CRITICAL REVISION NEEDED:\n{reviewer_feedback}\n\nORIGINAL:\n{base_worker_prompt}"

    # Inject recent worker failure memory
    recent_failures = _load_recent_failures(3)
    if recent_failures:
        failure_lines = ["# Recent Worker Failures (avoid repeating these mistakes):"]
        for f in recent_failures:
            failure_lines.append(f"- Gen {f['gen']} Worker {f['worker_id']} ({f['role']}): {f['error'][:150]}")
        base_worker_prompt += "\n\n" + "\n".join(failure_lines)

    worker_log_file = get_logs_dir(next_v) / f"worker_{w_id}_io.txt"

    compile_errors = []
    smoke_errors = []
    _last_reason = "unknown"
    for attempt in range(MAX_WORKER_RETRIES):
        ui.clear_io()
        ui.set_status(f"[{role}] coding for v{next_v}...", is_working=True)

        worker_prompt = substitute_template(worker_template, {
            "role": role,
            "worker_prompt": base_worker_prompt,
            "version": str(next_v),
        })

        # ── Timeout isolation: abort and retry if worker hangs for >WORKER_TIMEOUT sec ──
        try:
            await asyncio.wait_for(
                run_claude_query(
                    worker_prompt, context_files, ui,
                    f"WORKER {w_id} ({role})", worker_log_file, is_text_ui,
                    tools=["Bash", "Read", "Edit"],
                ),
                timeout=WORKER_TIMEOUT,
            )
        except asyncio.TimeoutError:
            _last_reason = f"timed out after {WORKER_TIMEOUT}s (attempt {attempt+1}/{MAX_WORKER_RETRIES})"
            ui.log_history(
                f"Worker {w_id} ({role}) timed out after {WORKER_TIMEOUT}s. Retrying with simpler task...",
                "warn",
            )
            base_worker_prompt += (
                "\n\nPREVIOUS ATTEMPT TIMED OUT. Start fresh with a minimal, focused implementation. "
                "Implement only the single most impactful change — do NOT try to do everything at once."
            )
            continue

        compile_errors = verify_code(next_dir)
        if compile_errors:
            _last_reason = f"compile error: {compile_errors[0][:200]}"
            base_worker_prompt += f"\n\nCRITICAL FIX: Fix syntax error:\n{compile_errors[0]}"
            continue

        smoke_errors = run_smoke_test(next_dir)
        if smoke_errors:
            _last_reason = f"smoke test error: {smoke_errors[0][:200]}"
            base_worker_prompt += f"\n\nCRITICAL FIX: Fix runtime error:\n{smoke_errors[0]}"
            continue

        return True

    # Worker failed all retries — record failure
    _record_worker_failure(next_v, w_id, role, _last_reason)
    return False


async def _execute_workers(tasks, worker_template, next_dir, next_v,
                            context_files, ui, is_text_ui, reviewer_feedback,
                            source_v=None):
    """Execute worker tasks. Tries parallel first, falls back to serial on failure."""
    if len(tasks) <= 1:
        # Single task — run directly
        return await _run_single_worker(
            tasks[0], 0, worker_template, next_dir, next_v,
            context_files, ui, is_text_ui, reviewer_feedback,
        )

    # Try parallel execution (capped at MAX_PARALLEL_WORKERS via semaphore)
    ui.log_history(f"Launching {len(tasks)} workers in parallel (max {MAX_PARALLEL_WORKERS} concurrent)...", "info")

    async def _guarded_worker(task, i):
        async with _get_worker_semaphore():
            return await _run_single_worker(
                task, i, worker_template, next_dir, next_v,
                context_files, ui, is_text_ui, reviewer_feedback,
            )

    coros = [_guarded_worker(task, i) for i, task in enumerate(tasks)]
    results = await asyncio.gather(*coros, return_exceptions=True)

    all_ok = all(r is True for r in results)
    if all_ok:
        return True

    # Parallel had issues — fall back to serial with fresh copy
    ui.log_history("Parallel execution had issues, retrying serially...", "warn")
    _source = source_v if source_v is not None else (next_v - 1 if next_v > 1 else 1)
    src_dir = get_bot_dir(_source)
    if next_dir.exists():
        shutil.rmtree(next_dir)
    shutil.copytree(src_dir, next_dir, ignore=_COPY_IGNORE)
    (next_dir / ".completed").unlink(missing_ok=True)

    for i, task in enumerate(tasks):
        ok = await _run_single_worker(
            task, i, worker_template, next_dir, next_v,
            context_files, ui, is_text_ui, reviewer_feedback,
        )
        if not ok:
            return False
    return True


# ──────────────────────────────────────────────
# Main Evolution Loop
# ──────────────────────────────────────────────

async def main_loop(ui, is_text_ui, no_daemon=False):
    os.makedirs(GRAVEYARD_DIR, exist_ok=True)

    if seed_initial_bots(ui):
        ui.log_history("Bootstrap complete: v1 to v6 initialized.", "success")
        # Commit seeded bots to git
        git_ensure_clean()
        for i in range(1, 7):
            tag = f"bot-v{i}"
            if not _git("tag", "-l", tag, check=False):
                _git("tag", tag, "-m", f"Bot v{i}: seeded from reference bot{i}")

    current_v = 1
    while True:
        target_dir = get_bot_dir(current_v)
        if target_dir.exists():
            if (target_dir / ".completed").exists():
                # Dual validation: .completed + git tag (seeded bots v1-v6 may lack tags)
                if current_v <= 6 or git_has_tag(current_v):
                    current_v += 1
                else:
                    # .completed exists but no git tag → false positive from copytree
                    ui.log_history(
                        f"v{current_v} has .completed but no git tag. Rolling back.", "warn")
                    shutil.rmtree(target_dir)
                    break
            else:
                ui.log_history(f"Incomplete v{current_v} detected. Rolling back.", "warn")
                shutil.rmtree(target_dir)
                break
        else:
            break

    if current_v == 1:
        ui.log_history("No bots found. Initializing Genesis Bot (v1)...", "info")
        while True:
            ui.set_status("Running Round 0 (Baseline Generation)...", is_working=True)
            os.makedirs(get_bot_dir(1), exist_ok=True)

            with open(PROMPTS_DIR / "initial_prompt.md") as f:
                prompt = f.read()
            instruction = prompt + "\n\nPlease write the full code for main.py, preflop.py, and postflop.py directly into bots/claude_v1/ directory."

            log_file = get_logs_dir(1) / "initial_generation_io.txt"

            genesis_ok = False
            for attempt in range(MAX_GENESIS_RETRIES):
                await run_claude_query(instruction, [], ui, f"GENESIS BOT (Try {attempt+1})", log_file, is_text_ui,
                                       tools=["Bash", "Read", "Edit"])

                compile_errors = verify_code(get_bot_dir(1))
                if compile_errors:
                    ui.log_history("Genesis v1 failed syntax check.", "warn")
                    instruction += f"\n\nCRITICAL FIX: Fix syntax error:\n{compile_errors[0]}"
                    continue

                smoke_errors = run_smoke_test(get_bot_dir(1))
                if smoke_errors:
                    ui.log_history("Genesis v1 failed smoke test.", "warn")
                    instruction += f"\n\nCRITICAL FIX: Fix runtime error:\n{smoke_errors[0]}"
                    continue

                (get_bot_dir(1) / ".completed").touch()
                git_commit_bot(1, 0, "genesis: initial bot from scratch")
                try:
                    from server.state import app_state
                    app_state.set_generation(1, 2)
                except Exception:
                    pass
                ui.log_history("Genesis v1 generated successfully.", "success")
                genesis_ok = True
                break

            if genesis_ok:
                break

            ui.log_history("Genesis bot failed 3 times. Retrying in 10s...", "warn")
            await asyncio.sleep(10)
            v1_dir = get_bot_dir(1)
            if v1_dir.exists():
                shutil.rmtree(v1_dir)
    else:
        current_v -= 1
        ui.log_history(f"Resumed successfully from v{current_v}", "success")

    git_ensure_clean()

    ref_context = get_reference_context()
    ratings = load_ratings()

    # ── Evolution metrics tracking ──
    loop_start_time = time.time()
    total_gens = 0
    total_success = 0
    fail_count = 0
    gen_start_time = time.time()
    initial_rating = None

    while True:
        # ── Consecutive failure cooldown ──
        if fail_count >= COOLDOWN_THRESHOLD:
            ui.log_history(f"{fail_count} consecutive failures. Cooling down for 1 hour...", "warn")
            await asyncio.sleep(COOLDOWN_SECONDS)
            # Reset counter but keep current_v — just retry from where we are
            fail_count = 0

        # Trim experience pool to prevent unbounded growth
        trim_experience_pool(max_entries=8)

        active_bots = get_active_bots()
        ui.update_eval_table(ratings, active_bots)

        # Reaper: cull weakest by conservative rating (r - 2*rd)
        if len(active_bots) > MAX_ACTIVE_BOTS:
            ui.log_history(f"Pool size {len(active_bots)} exceeds limit {MAX_ACTIVE_BOTS}. The Reaper approaches...", "warn")

            active_ratings = [(b, ratings.get(b, Glicko2Player())) for b in active_bots]
            active_ratings.sort(key=lambda x: x[1].r - 2 * x[1].rd)
            weakest_bot = active_ratings[0][0]
            weakest_p = active_ratings[0][1]

            ui.log_history(f"💀 Reaper culled {weakest_bot} (r={weakest_p.r:.1f}, rd={weakest_p.rd:.1f})", "error")
            shutil.move(BOTS_DIR / weakest_bot, GRAVEYARD_DIR / weakest_bot)
            if weakest_bot in ratings:
                del ratings[weakest_bot]
            active_bots = get_active_bots()
            ui.update_eval_table(ratings, active_bots)

        next_v = current_v + 1

        # Git tag collision guard: if bot-v{next_v} tag already exists (e.g. from a
        # previous interrupted run that committed but didn't advance current_v),
        # skip forward to the next available version.
        if git_has_tag(next_v):
            old_next = next_v
            while git_has_tag(next_v):
                next_v += 1
            ui.log_history(
                f"Tag bot-v{old_next} already exists. Advancing next_v to {next_v}.", "warn")
            current_v = next_v - 1

        # Stagnation detection — use LLM for intelligent analysis when stagnation suspected
        source_v = current_v
        stag_count = git_get_stagnation_count(f"claude_v{current_v}", ratings)
        stagnation_info = "No stagnation detected. Continue from latest version."
        if stag_count >= 2:
            # Use LLM to analyze whether stagnation is real or Glicko noise
            stag_result = await _analyze_stagnation(source_v, active_bots, ratings, ui, is_text_ui)
            if stag_result and stag_result.get("is_stagnant"):
                rec = stag_result.get("recommendation", "branch")
                confidence = stag_result.get("confidence", "unknown")
                reason = stag_result.get("reason", "No improvement trend detected")
                stagnation_info = (
                    f"⚠️ STAGNATION (confidence: {confidence}): {reason}\n"
                    f"Recommendation: {rec}\n"
                    f"Available bots:\n"
                )
                for b in active_bots:
                    p = ratings.get(b, Glicko2Player())
                    stagnation_info += f"  {b}: r={p.r:.1f} rd={p.rd:.1f}\n"

                # If LLM recommends crossover, force stag_count to trigger crossover path
                if rec == "crossover":
                    stag_count = max(stag_count, 3)

                # If LLM recommends a specific branch target, hint to Master
                branch_hint = stag_result.get("branch_from")
                if branch_hint:
                    stagnation_info += f"Analyst suggests branching from: {branch_hint}\n"

                ui.log_history(
                    f"⚠️ Stagnation ({stag_count} gens, {confidence}). LLM recommends: {rec}",
                    "warn",
                )
            elif stag_result and not stag_result.get("is_stagnant"):
                # LLM says it's noise
                stagnation_info = (
                    f"Rating variation detected but likely Glicko noise. "
                    f"({stag_result.get('reason', 'Variance within expected range')}) "
                    "Continue from latest version."
                )
                ui.log_history(f"Stagnation check: LLM says Glicko noise. Continuing.", "info")
            else:
                # LLM analysis failed — fallback to hardcoded logic
                stagnation_info = (
                    f"⚠️ STAGNATION DETECTED: {stag_count} consecutive non-improving generations.\n"
                    f"Available bots to branch from:\n"
                )
                for b in active_bots:
                    p = ratings.get(b, Glicko2Player())
                    stagnation_info += f"  {b}: r={p.r:.1f} rd={p.rd:.1f}\n"
                stagnation_info += "Consider setting `branch_from` in your output to restart from a different ancestor."
                ui.log_history(
                    f"⚠️ Stagnation ({stag_count} gens). LLM analysis unavailable, using fallback.",
                    "warn",
                )

        # Severe stagnation → try crossover between top-2 bots
        if stag_count >= 3 and len(active_bots) >= 2:
            top_bots = sorted(active_bots, key=lambda b: ratings.get(b, Glicko2Player()).r, reverse=True)[:2]
            parent_a = int(top_bots[0].split("_v")[1])
            parent_b = int(top_bots[1].split("_v")[1])
            ui.log_history(f"🔥 Severe stagnation. Crossover: v{parent_a} × v{parent_b} → v{next_v}", "warn")

            # Prepare target directory from parent A baseline
            next_dir = get_bot_dir(next_v)
            if next_dir.exists():
                shutil.rmtree(next_dir)
            shutil.copytree(get_bot_dir(parent_a), next_dir, ignore=_COPY_IGNORE)
            (next_dir / ".completed").unlink(missing_ok=True)

            crossover_ok = await _run_crossover(parent_a, parent_b, next_v, ui, is_text_ui)
            if crossover_ok:
                # Run decision tests on crossover bot
                decision_detail = run_decision_test_details(next_dir)
                decision_pass_rate = decision_detail["pass_rate"]
                if decision_pass_rate >= MIN_CROSSOVER_DECISION_RATE and not decision_detail.get("critical_failures"):
                    (next_dir / ".completed").touch()
                    git_commit_bot(next_v, parent_a, f"crossover: v{parent_a}×v{parent_b}", parent2_v=parent_b)
                    try:
                        from server.state import app_state
                        app_state.set_generation(next_v, next_v + 1)
                    except Exception:
                        pass
                    current_v = next_v
                    fail_count = 0
                    total_gens += 1
                    total_success += 1

                    # Update metrics (crossover skips normal pipeline metrics)
                    gen_elapsed = time.time() - gen_start_time
                    current_rating = ratings.get(f"claude_v{current_v}", Glicko2Player()).r
                    if initial_rating is None:
                        initial_rating = current_rating
                    metrics = {
                        "current_v": current_v,
                        "next_v": current_v + 1,
                        "total_time_s": time.time() - loop_start_time,
                        "avg_gen_time_s": (time.time() - loop_start_time) / max(1, total_gens),
                        "success_rate": total_success / max(1, total_gens),
                        "total_gens": total_gens,
                        "total_success": total_success,
                        "fail_count": fail_count,
                        "rating_trend": current_rating - initial_rating,
                    }
                    ui.update_metrics(metrics)
                    ui.log_history(f"Crossover v{next_v} accepted! (decision tests: {decision_pass_rate:.0%}, {gen_elapsed:.0f}s)", "success")
                    gen_start_time = time.time()
                    continue
                else:
                    crit_count = len(decision_detail.get("critical_failures", []))
                    ui.log_history(f"Crossover v{next_v} failed decision tests ({decision_pass_rate:.0%}, critical failures={crit_count}).", "warn")
            else:
                ui.log_history("Crossover failed. Falling back to normal evolution.", "warn")

            # Clean up failed crossover directory and logs
            if next_dir.exists():
                shutil.rmtree(next_dir)
            logs_dir = get_logs_dir(next_v)
            if logs_dir.exists():
                shutil.rmtree(logs_dir)

        ui.set_header(f"🔥 Antigravity Glicko-2 Evolution: v{source_v} ➡️ v{next_v} 🔥")

        # ── Pipeline checkpoint recovery ──
        # If a previous run was killed mid-gen for this exact next_v, skip Master and
        # resume directly in the intra-gen loop from the saved pipeline stage.
        _checkpoint = read_pipeline_checkpoint()
        resume_stage = None
        tasks_data = None
        reviewer_feedback_from_ckpt = ""
        my_bot = f"claude_v{current_v}"
        if _checkpoint and _checkpoint.get("next_v") == next_v and _checkpoint.get("master_plan"):
            resume_stage = _checkpoint.get("stage")
            tasks_data = _checkpoint["master_plan"]
            source_v = _checkpoint.get("source_v", source_v)
            reviewer_feedback_from_ckpt = _checkpoint.get("reviewer_feedback", "")
            ui.log_history(
                f"Resuming v{next_v} from checkpoint stage='{resume_stage}' "
                f"(source=v{source_v}). Skipping Master + eval.",
                "info",
            )

        if tasks_data is not None:
            pass  # Checkpoint restored tasks_data; skip daemon eval + master
        elif not no_daemon:
            ui.set_status(f"Pipelining daemon eval + Master analysis for v{current_v}...", is_working=True)
            ui.log_history(f"v{current_v} pipelining daemon eval + Master analysis...", "info")

            # Run match analysis (pipelined with daemon eval)
            match_analysis = await _analyze_recent_matches(source_v, ui, is_text_ui)

            # SATLUTION-style performance verification (sequential with match analysis)
            ui.log_history("Running performance verification...", "info")
            perf_verification = await _run_performance_verification(source_v, ratings, ui, is_text_ui)

            # Launch Master analysis concurrently — it reads files via tools, doesn't need final ratings
            master_task = asyncio.create_task(
                _run_master_analysis(source_v, next_v, stagnation_info, ui, is_text_ui,
                                     match_analysis, perf_verification)
            )

            # Wait for daemon evaluation (async, non-blocking)
            eval_ok = await wait_for_daemon_eval(my_bot)
            if not eval_ok:
                ui.log_history(f"Daemon eval timeout for v{current_v}, using preliminary ratings.", "warn")
            ratings = load_ratings()
            ui.update_eval_table(ratings, active_bots)

            # Await Master result (may already be done)
            tasks_data = await master_task
        else:
            ui.set_status(f"v{current_v} inline evaluation...", is_working=True)
            ui.log_history(f"v{current_v} entering inline Glicko-2 evaluation...", "info")

            opponents_to_play = [b for b in active_bots if b != my_bot]

            if my_bot not in ratings:
                ratings[my_bot] = Glicko2Player()

            if opponents_to_play:
                sys.path.insert(0, str((CORE_DIR / ".." / "engine").resolve()))
                from battle import mirror_battle

                my_results = []
                for opp in opponents_to_play:
                    if opp not in ratings:
                        ratings[opp] = Glicko2Player()
                    match_wins, draws, n_played, _ = mirror_battle(
                        str(BOTS_DIR / my_bot / "main.py"),
                        str(BOTS_DIR / opp / "main.py"),
                        n_games=5, verbose=False, save_log=False
                    )
                    w_a, w_b = match_wins[0], match_wins[1]
                    ui.log_history(f"  vs {opp}: {w_a}-{w_b}-{draws}", "info")
                    for _ in range(w_a):
                        my_results.append((ratings[opp], 1.0))
                    for _ in range(w_b):
                        my_results.append((ratings[opp], 0.0))
                    for _ in range(draws):
                        my_results.append((ratings[opp], 0.5))

                if my_results:
                    ratings[my_bot] = update_rating_period(ratings[my_bot], my_results)

            # Run match analysis + performance verification then Master after inline eval
            match_analysis = await _analyze_recent_matches(source_v, ui, is_text_ui)
            perf_verification = await _run_performance_verification(source_v, ratings, ui, is_text_ui)
            tasks_data = await _run_master_analysis(source_v, next_v, stagnation_info, ui, is_text_ui,
                                                    match_analysis, perf_verification)

        # 2. Master result check — auto-retry on failure
        if tasks_data is None:
            ui.log_history("Master failed. Will retry next generation.", "error")
            fail_count += 1
            await asyncio.sleep(5)
            continue

        # Handle Master's branch_from decision
        # Only change source_v (which bot to copy from), NOT current_v (version counter).
        # This keeps next_v monotonically increasing and prevents version collisions.
        if tasks_data.get("branch_from"):
            bf = tasks_data["branch_from"]
            try:
                branch_v = int(bf.split("_v")[1])
                target_dir = get_bot_dir(branch_v)
                # Validate target exists and is genuinely completed (git tag verification)
                if not target_dir.exists():
                    ui.log_history(f"branch_from target {bf} not found. Ignoring.", "warn")
                elif not (target_dir / ".completed").exists():
                    ui.log_history(f"branch_from target {bf} not completed. Ignoring.", "warn")
                elif branch_v > 6 and not git_has_tag(branch_v):
                    ui.log_history(
                        f"branch_from target {bf} has .completed but no git tag. Ignoring.", "warn")
                elif branch_v != source_v:
                    ui.log_history(
                        f"Master chose to branch from {bf} instead of v{source_v}",
                        "warn",
                    )
                    source_v = branch_v
            except (ValueError, IndexError):
                ui.log_history(f"Invalid branch_from value: {bf}", "warn")

        generation_approved = False
        # Use reviewer_feedback from checkpoint if resuming, otherwise start fresh
        reviewer_feedback = reviewer_feedback_from_ckpt if resume_stage else ""
        # Intra-generation iteration: re-run workers within same gen if Critic score < 6
        # MAX_INTRA_GEN_ITERS controls how many Critic-driven retries are allowed (max 2)
        # The outer loop range(3) covers (initial + 2 retries), matching MAX_INTRA_GEN_ITERS

        for generation_attempt in range(MAX_INTRA_GEN_ITERS + 1):
            if generation_approved:
                break

            ui.log_history(f"Generation Pipeline (Attempt {generation_attempt+1}/{MAX_INTRA_GEN_ITERS+1})", "info")

            # On first attempt only, respect resume_stage from checkpoint.
            # On subsequent attempts (intra-gen retries) always run all stages fresh.
            skip_to = resume_stage if generation_attempt == 0 else None

            # Prepare: copy source → next (skip if checkpoint says already done)
            if not _stage_done(skip_to, "prepared"):
                next_dir = get_bot_dir(next_v)
                if next_dir.exists():
                    shutil.rmtree(next_dir)
                shutil.copytree(get_bot_dir(source_v), next_dir, ignore=_COPY_IGNORE)
                (next_dir / ".completed").unlink(missing_ok=True)
                write_pipeline_checkpoint(next_v, source_v, "prepared", tasks_data,
                                          reviewer_feedback, generation_attempt)
            else:
                next_dir = get_bot_dir(next_v)
                ui.log_history("[Resume] Skipping prepare (already done)", "info")

            with open(PROMPTS_DIR / "worker_prompt.md") as f:
                worker_template = f.read()

            # Workers: skip if checkpoint says already done
            if not _stage_done(skip_to, "workers_done"):
                # Build worker context files — workers read bot files via Read tool
                worker_context_files = []

                workers_succeeded = await _execute_workers(
                    tasks_data["tasks"], worker_template, next_dir, next_v,
                    worker_context_files, ui, is_text_ui, reviewer_feedback,
                    source_v=source_v,
                )

                if not workers_succeeded:
                    continue  # Retry with fresh copy within generation_attempt loop

                write_pipeline_checkpoint(next_v, source_v, "workers_done", tasks_data,
                                          reviewer_feedback, generation_attempt)
            else:
                workers_succeeded = True
                ui.log_history("[Resume] Skipping workers (already done)", "info")

            # Single-file size constraint + decision tests (skip if checkpoint says passed)
            if not _stage_done(skip_to, "quality_passed"):
                total_lines, oversized_files = check_code_size(next_dir)
                if oversized_files:
                    details = ", ".join(f"{name}={lines}" for name, lines in oversized_files)
                    reviewer_feedback = (
                        f"These files exceed 1000 lines and must be split into modules: {details}. "
                        "Keep main.py as the entry point, extract logic into separate .py files."
                    )
                    ui.log_history(f"File size check failed: {details}", "warn")
                    continue

                # Decision scenario tests — reject catastrophic blunders
                decision_detail = run_decision_test_details(next_dir)
                decision_pass_rate = decision_detail["pass_rate"]
                critical_failures = decision_detail.get("critical_failures", [])
                ui.log_history(f"Decision tests: {decision_pass_rate:.0%} pass rate", "info")
                if decision_pass_rate < MIN_DECISION_PASS_RATE or critical_failures:
                    reviewer_feedback = (
                        f"Bot failed decision tests ({decision_pass_rate:.0%} pass rate, "
                        f"{len(critical_failures)} critical failures). "
                        "Review fundamental strategy: don't fold premium hands, don't bluff with missed draws facing big bets."
                    )
                    ui.log_history("Decision test threshold not met, requesting revision.", "warn")
                    continue

                write_pipeline_checkpoint(next_v, source_v, "quality_passed", tasks_data,
                                          reviewer_feedback, generation_attempt)
            else:
                ui.log_history("[Resume] Skipping quality gates (already passed)", "info")

            # ── Code Reviewer (correctness + role boundaries) ──
            reviewer_data = {}
            if not _stage_done(skip_to, "reviewed"):
                ui.set_status(f"Code Reviewer analyzing v{next_v}...", is_working=True)
                with open(PROMPTS_DIR / "reviewer_prompt.md") as f:
                    reviewer_prompt = f.read()

                reviewer_log_file = get_logs_dir(next_v) / "reviewer_io.txt"
                reviewer_prompt = substitute_template(reviewer_prompt, {
                    "master_plan": json.dumps(tasks_data, indent=2),
                    "version": str(next_v),
                    "parent_version": str(source_v),
                })

                reviewer_approved = False
                for review_attempt in range(MAX_REVIEWER_RETRIES):
                    ui.clear_io()
                    reviewer_output, _, _ = await run_claude_query(reviewer_prompt, [], ui, "LEAD CODE REVIEWER", reviewer_log_file, is_text_ui, tools=["Bash", "Read"])
                    reviewer_data = parse_json_output(reviewer_output)

                    if reviewer_data and "approved" in reviewer_data:
                        if reviewer_data["approved"]:
                            reviewer_approved = True
                            qs = reviewer_data.get("quality_score", 0)
                            if qs:
                                ui.log_history(f"Reviewer quality score: {qs}/10", "info")
                            risks = reviewer_data.get("risk_areas", [])
                            if risks:
                                ui.log_history(f"Risk areas: {'; '.join(risks)}", "warn")
                        else:
                            reviewer_feedback = reviewer_data.get("feedback", "")
                            ui.log_history(f"Reviewer rejected (attempt {review_attempt+1}): {reviewer_feedback[:80]}", "warn")
                        break
                else:
                    reviewer_feedback = "Reviewer failed to produce valid output. Please review and retry."

                if not reviewer_approved:
                    continue  # Retry workers with reviewer_feedback injected

                write_pipeline_checkpoint(next_v, source_v, "reviewed", tasks_data,
                                          reviewer_feedback, generation_attempt)
            else:
                reviewer_approved = True
                ui.log_history("[Resume] Skipping reviewer (already approved)", "info")

            # ── Strategy Critic (poker-specific quality gate) ──
            if not _stage_done(skip_to, "critic_checked"):
                ui.set_status(f"Strategy Critic evaluating v{next_v}...", is_working=True)
                master_plan_str = json.dumps(tasks_data, indent=2)
                critic_data = await _run_critic(next_v, source_v, master_plan_str, ui, is_text_ui)
                critic_score = critic_data.get("score", 6)
                critic_approved = critic_data.get("approved", True)

                ui.log_history(f"Critic score: {critic_score}/10 ({'approved' if critic_approved else 'rejected'})", "info")
                if critic_data.get("local_optima_warning"):
                    ui.log_history(f"⚠️ Local optima warning: {critic_data.get('local_optima_reason', '')}", "warn")

                if not critic_approved and generation_attempt < MAX_INTRA_GEN_ITERS:
                    # Inject critic feedback for next intra-gen iteration
                    critic_feedback = critic_data.get("feedback", "")
                    reviewer_feedback = (
                        f"[CRITIC FEEDBACK — score {critic_score}/10, attempt {generation_attempt+1}]: "
                        f"{critic_feedback}\n"
                        "The strategy change was insufficient — implement a more impactful improvement."
                    )
                    ui.log_history(f"Critic rejected (score {critic_score}/10). Retrying with new approach...", "warn")
                    continue

                write_pipeline_checkpoint(next_v, source_v, "critic_checked", tasks_data,
                                          reviewer_feedback, generation_attempt)
            else:
                ui.log_history("[Resume] Skipping critic (already checked)", "info")

            # ── Generation approved by both Reviewer and Critic ──
            generation_approved = True
            summary = reviewer_data.get("change_summary", "")
            if summary:
                with locked_file(EXPERIENCE_FILE, "a") as ep:
                    ep.write(f"\n- **v{source_v} -> v{next_v} review**: {summary}\n")

        # Workers failed → auto-retry (not break)
        if not workers_succeeded:
            ui.log_history("Workers failed. Retrying from scratch.", "error")
            fail_count += 1
            continue

        # Reviewer rejected → auto-retry
        if not generation_approved:
            ui.log_history(f"Generation v{next_v} not approved. Will retry.", "warn")
            fail_count += 1
            continue

        # ── Generation approved! ──
        (next_dir / ".completed").touch()

        # Git commit + tag
        source_bot = f"claude_v{source_v}"
        next_bot = f"claude_v{next_v}"
        strategy_tag = tasks_data.get("analysis", "")[:80] if tasks_data.get("analysis") else ""
        my_p = ratings.get(my_bot, Glicko2Player())
        git_commit_bot(
            next_v, source_v, strategy_tag,
            rating_info=f"rating: r={my_p.r:.1f} rd={my_p.rd:.1f}"
        )
        clear_pipeline_checkpoint()

        try:
            from server.state import app_state
            app_state.set_generation(next_v, next_v + 1)
        except Exception:
            pass

        current_v = next_v
        fail_count = 0
        total_gens += 1
        total_success += 1

        gen_elapsed = time.time() - gen_start_time
        current_rating = ratings.get(f"claude_v{current_v}", Glicko2Player()).r
        if initial_rating is None:
            initial_rating = current_rating

        ui.log_history(f"Successfully evolved to v{current_v}! ({gen_elapsed:.0f}s)", "success")
        if hasattr(ui, 'reset_gen_cost'):
            ui.reset_gen_cost()

        # Consolidate experience pool every 3 generations
        if total_gens % CONSOLIDATE_EVERY_N_GENS == 0:
            ui.log_history("Consolidating experience pool...", "info")
            await _consolidate_experience_pool(ui, is_text_ui)

        # Generate evolution report every 5 generations
        if total_gens % REPORT_EVERY_N_GENS == 0:
            ui.log_history("Generating evolution report...", "info")
            await _generate_evolution_report(current_v, total_gens, ratings, ui, is_text_ui)

        # Update metrics
        metrics = {
            "current_v": current_v,
            "next_v": current_v + 1,
            "total_time_s": time.time() - loop_start_time,
            "avg_gen_time_s": (time.time() - loop_start_time) / max(1, total_gens),
            "success_rate": total_success / max(1, total_gens),
            "total_gens": total_gens,
            "total_success": total_success,
            "fail_count": fail_count,
            "rating_trend": current_rating - initial_rating,
        }
        ui.update_metrics(metrics)
        gen_start_time = time.time()

    ui.set_status("Evolution Complete.", is_working=False)
    ui.log_history("Matrix simulation concluded.", "success")
    await asyncio.sleep(5)
