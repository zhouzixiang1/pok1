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

WORKSPACE = Path("evolution_workspace")
PROJECT_ROOT = WORKSPACE.parent
_COPY_IGNORE = shutil.ignore_patterns('__pycache__', '*.pyc')
PROMPTS_DIR = WORKSPACE / "prompts"
RESULTS_DIR = WORKSPACE / "results"
BOTS_DIR = Path("bots")
EXPERIENCE_FILE = WORKSPACE / "experience_pool.md"
REFERENCE_DIR = WORKSPACE / "reference_bots"
GRAVEYARD_DIR = BOTS_DIR / "graveyard"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
STATS_FILE = RESULTS_DIR / "elo_daemon_stats.json"

MAX_ACTIVE_BOTS = 30

# Add workspace to sys.path for glicko2 import
sys.path.insert(0, str(WORKSPACE.resolve()))
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
        with open(RATINGS_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
        return {name: Glicko2Player.from_dict(d) for name, d in data.items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_daemon_stats():
    """Load daemon stats."""
    if STATS_FILE.exists():
        with open(STATS_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
        return data
    return {"pairs": {}, "total_periods": 0}


async def wait_for_daemon_eval(bot_name, timeout=600, min_matches=20, max_rd=40):
    """Wait for daemon to evaluate a new bot (async, non-blocking).

    Requires both sufficient matches AND low rating deviation for confidence.
    Uses mtime caching to avoid redundant disk reads.
    """
    start = time.time()
    cached_stats = None
    cached_ratings = None
    stats_mtime = 0
    ratings_mtime = 0

    while time.time() - start < timeout:
        # Only re-read files if they've been modified
        if STATS_FILE.exists():
            mt = os.path.getmtime(STATS_FILE)
            if mt != stats_mtime:
                stats_mtime = mt
                cached_stats = load_daemon_stats()
        if cached_stats is None:
            cached_stats = {"pairs": {}, "total_periods": 0}

        if RATINGS_FILE.exists():
            mt = os.path.getmtime(RATINGS_FILE)
            if mt != ratings_mtime:
                ratings_mtime = mt
                cached_ratings = load_ratings()
        if cached_ratings is None:
            cached_ratings = {}

        matches = sum(v for k, v in cached_stats.get("pairs", {}).items() if bot_name in k)
        rd = cached_ratings.get(bot_name, Glicko2Player()).rd
        if matches >= min_matches and rd <= max_rd:
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
        daemon_script = str(WORKSPACE / "elo_daemon.py")
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


def git_ensure_clean():
    """Stage + commit all tracked files as checkpoint."""
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
    """Commit a completed bot generation (bot code + ratings + experience pool)."""
    parent_line = f"parent: claude_v{source_v}"
    if parent2_v is not None:
        parent_line += f"\nparent2: claude_v{parent2_v}"
    msg = (
        f"evolve: v{source_v} → v{version}\n\n"
        f"{parent_line}\n"
        f"strategy: {strategy_tag}\n"
        f"{rating_info}"
    )
    _git("add", "-A")
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

async def run_claude_query(prompt, context_files, ui, role_name, log_file_path, is_text_ui):
    """Run a Claude query via the Agent SDK with cost tracking and typed streaming."""
    full_prompt = prompt
    if context_files:
        full_prompt += "\n\n# Context Files:\n"
        for cf in context_files:
            if os.path.exists(cf):
                with open(cf, 'r') as f:
                    full_prompt += f"\n--- {cf} ---\n{f.read()}\n"

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
        model="sonnet",
        permission_mode="bypassPermissions",
        cwd=str(Path(__file__).parent.parent),  # project root
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


def check_code_size(directory, max_lines_per_file=1000):
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
        ["python", str(WORKSPACE / "smoke_tester.py"), main_path],
        capture_output=True, text=True
    )
    if proc.returncode != 0:
        return [proc.stderr.strip() or proc.stdout.strip()]
    return []


def run_decision_tests(directory):
    """Run standard decision scenarios. Returns pass rate (0.0 - 1.0)."""
    main_path = os.path.join(directory, "main.py")
    if not os.path.exists(main_path):
        return 0.0
    from decision_tester import run_decision_tests as _run
    return _run(main_path, verbose=False)


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

async def _run_master_analysis(source_v, next_v, stagnation_info, ui, is_text_ui):
    """Run Master analysis — can run concurrently with daemon evaluation."""
    master_prompt = (PROMPTS_DIR / "master_prompt.md").read_text()
    master_prompt = master_prompt.replace("{stagnation_info}", stagnation_info)
    master_ctx = (
        f"Current evolution: v{source_v} → v{next_v}\n"
        f"Bot directory: bots/claude_v{source_v}/\n"
        f"Ratings file: evolution_workspace/results/glicko_ratings.json\n"
        f"Rating history: evolution_workspace/results/rating_history.jsonl\n"
    )
    master_log_file = get_logs_dir(next_v) / "master_io.txt"

    for attempt in range(3):
        ui.clear_io()
        output, _, _ = await run_claude_query(
            master_prompt + "\n" + master_ctx, [], ui,
            f"MASTER (Try {attempt+1})", master_log_file, is_text_ui,
        )
        data = parse_json_output(output)
        if data and "tasks" in data:
            ui.log_history("Master analysis complete.", "success")
            return data
        ui.log_history("Master output malformed JSON. Retrying...", "warn")
        await asyncio.sleep(2)

    ui.log_history("Master failed to plan after 3 retries.", "error")
    return None


async def _consolidate_experience_pool(ui, is_text_ui):
    """Use LLM to deduplicate and consolidate the experience pool.

    Reads the current experience_pool.md, asks LLM to merge redundant entries,
    and writes back a consolidated version. Runs every 3 generations.
    """
    if not EXPERIENCE_FILE.exists():
        return

    with open(EXPERIENCE_FILE, "r") as ef:
        fcntl.flock(ef, fcntl.LOCK_SH)
        content = ef.read()
        fcntl.flock(ef, fcntl.LOCK_UN)
    if not content or len(content.split("\n")) < 20:
        return  # Too short to bother consolidating

    consolidate_prompt = (
        "You are an Experience Pool Consolidator. Your job is to clean up the experience pool file.\n\n"
        "RULES:\n"
        "1. Read the current experience_pool.md file.\n"
        "2. Merge duplicate or near-duplicate lessons into single, concise entries.\n"
        "3. Keep the most recent/relevant version of each lesson.\n"
        "4. Remove entries that have been superseded by newer findings.\n"
        "5. Preserve the markdown format and generation headers.\n"
        "6. Keep the total file under 60 lines.\n"
        "7. Output ONLY the consolidated markdown content — no explanation, no code fences.\n\n"
        f"Edit the file in place: {EXPERIENCE_FILE}\n"
    )
    log_file = get_logs_dir(0) / "experience_consolidation_io.txt"

    try:
        ui.clear_io()
        output, _, _ = await run_claude_query(
            consolidate_prompt, [], ui,
            "EXPERIENCE CONSOLIDATOR", log_file, is_text_ui,
        )
        if output and len(output.strip()) > 50:
            ui.log_history("Experience pool consolidated.", "success")
        else:
            ui.log_history("Experience pool consolidation produced no output.", "warn")
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


async def _run_crossover(parent_a_v, parent_b_v, target_v, ui, is_text_ui):
    """Run crossover between two elite bots to create a new child bot."""
    crossover_prompt = (PROMPTS_DIR / "crossover_prompt.md").read_text()
    crossover_prompt = crossover_prompt.replace("{parent_a_version}", str(parent_a_v))
    crossover_prompt = crossover_prompt.replace("{parent_b_version}", str(parent_b_v))
    crossover_prompt = crossover_prompt.replace("{version}", str(target_v))

    target_dir = get_bot_dir(target_v)
    log_file = get_logs_dir(target_v) / "crossover_io.txt"

    for attempt in range(3):
        ui.clear_io()
        ui.set_status(f"Crossover v{parent_a_v}×v{parent_b_v}→v{target_v} (Try {attempt+1})", is_working=True)
        await run_claude_query(
            crossover_prompt, [], ui,
            f"CROSSOVER v{parent_a_v}×v{parent_b_v}→v{target_v}",
            log_file, is_text_ui,
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

    worker_log_file = get_logs_dir(next_v) / f"worker_{w_id}_io.txt"

    for attempt in range(4):
        ui.clear_io()
        ui.set_status(f"[{role}] coding for v{next_v}...", is_working=True)

        worker_prompt = worker_template.replace("{role}", role).replace(
            "{worker_prompt}", base_worker_prompt
        ).replace("{version}", str(next_v))

        await run_claude_query(
            worker_prompt, context_files, ui,
            f"WORKER {w_id} ({role})", worker_log_file, is_text_ui,
        )

        compile_errors = verify_code(next_dir)
        if compile_errors:
            base_worker_prompt += f"\n\nCRITICAL FIX: Fix syntax error:\n{compile_errors[0]}"
            continue

        smoke_errors = run_smoke_test(next_dir)
        if smoke_errors:
            base_worker_prompt += f"\n\nCRITICAL FIX: Fix runtime error:\n{smoke_errors[0]}"
            continue

        return True

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

    # Try parallel execution
    ui.log_history(f"Launching {len(tasks)} workers in parallel...", "info")
    coros = [
        _run_single_worker(
            task, i, worker_template, next_dir, next_v,
            context_files, ui, is_text_ui, reviewer_feedback,
        )
        for i, task in enumerate(tasks)
    ]
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
            for attempt in range(3):
                await run_claude_query(instruction, [], ui, f"GENESIS BOT (Try {attempt+1})", log_file, is_text_ui)

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
        if fail_count >= 3:
            ui.log_history(f"{fail_count} consecutive failures. Cooling down for 1 hour...", "warn")
            await asyncio.sleep(3600)
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
                decision_pass_rate = run_decision_tests(next_dir)
                if decision_pass_rate >= 0.6:
                    (next_dir / ".completed").touch()
                    git_commit_bot(next_v, parent_a, f"crossover: v{parent_a}×v{parent_b}", parent2_v=parent_b)
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
                    ui.log_history(f"Crossover v{next_v} failed decision tests ({decision_pass_rate:.0%}).", "warn")
            else:
                ui.log_history("Crossover failed. Falling back to normal evolution.", "warn")

            # Clean up failed crossover directory and logs
            if next_dir.exists():
                shutil.rmtree(next_dir)
            logs_dir = get_logs_dir(next_v)
            if logs_dir.exists():
                shutil.rmtree(logs_dir)

        ui.set_header(f"🔥 Antigravity Glicko-2 Evolution: v{source_v} ➡️ v{next_v} 🔥")

        # 1. Wait for daemon evaluation or run inline — pipelined with Master analysis
        my_bot = f"claude_v{current_v}"

        if not no_daemon:
            ui.set_status(f"Pipelining daemon eval + Master analysis for v{current_v}...", is_working=True)
            ui.log_history(f"v{current_v} pipelining daemon eval + Master analysis...", "info")

            # Launch Master analysis concurrently — it reads files via tools, doesn't need final ratings
            master_task = asyncio.create_task(
                _run_master_analysis(source_v, next_v, stagnation_info, ui, is_text_ui)
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
                sys.path.insert(0, str((WORKSPACE / ".." / "engine").resolve()))
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

            # Run Master after inline eval
            tasks_data = await _run_master_analysis(source_v, next_v, stagnation_info, ui, is_text_ui)

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
        reviewer_feedback = ""

        for generation_attempt in range(3):
            if generation_approved:
                break

            ui.log_history(f"Generation Pipeline (Attempt {generation_attempt+1})", "info")

            next_dir = get_bot_dir(next_v)
            if next_dir.exists():
                shutil.rmtree(next_dir)
            shutil.copytree(get_bot_dir(source_v), next_dir, ignore=_COPY_IGNORE)
            (next_dir / ".completed").unlink(missing_ok=True)

            with open(PROMPTS_DIR / "worker_prompt.md") as f:
                worker_template = f.read()

            # Build worker context files — workers read bot files via Read tool
            worker_context_files = []

            workers_succeeded = await _execute_workers(
                tasks_data["tasks"], worker_template, next_dir, next_v,
                worker_context_files, ui, is_text_ui, reviewer_feedback,
                source_v=source_v,
            )

            if not workers_succeeded:
                continue  # Retry with fresh copy within generation_attempt loop

            # Single-file size constraint — ensure readability
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
            decision_pass_rate = run_decision_tests(next_dir)
            ui.log_history(f"Decision tests: {decision_pass_rate:.0%} pass rate", "info")
            if decision_pass_rate < 0.7:
                reviewer_feedback = (
                    f"Bot failed decision tests ({decision_pass_rate:.0%} pass rate). "
                    "Review fundamental strategy: don't fold premium hands, don't bluff with missed draws facing big bets."
                )
                ui.log_history("Decision test threshold not met, requesting revision.", "warn")
                continue

            ui.set_status(f"Code Reviewer analyzing v{next_v}...", is_working=True)
            with open(PROMPTS_DIR / "reviewer_prompt.md") as f:
                reviewer_prompt = f.read()

            reviewer_log_file = get_logs_dir(next_v) / "reviewer_io.txt"
            reviewer_prompt = reviewer_prompt.replace("{master_plan}", json.dumps(tasks_data, indent=2))
            reviewer_prompt = reviewer_prompt.replace("{version}", str(next_v))
            reviewer_prompt = reviewer_prompt.replace("{parent_version}", str(source_v))

            for review_attempt in range(3):
                ui.clear_io()
                reviewer_output, _, _ = await run_claude_query(reviewer_prompt, [], ui, "LEAD CODE REVIEWER", reviewer_log_file, is_text_ui)
                reviewer_data = parse_json_output(reviewer_output)

                if reviewer_data and "approved" in reviewer_data:
                    if reviewer_data["approved"]:
                        generation_approved = True
                        # Log quality score and change summary
                        qs = reviewer_data.get("quality_score", 0)
                        if qs:
                            ui.log_history(f"Quality score: {qs}/10", "info")
                        summary = reviewer_data.get("change_summary", "")
                        if summary:
                            with open(EXPERIENCE_FILE, "a") as ep:
                                fcntl.flock(ep, fcntl.LOCK_EX)
                                ep.write(f"\n- **v{source_v} -> v{next_v} review**: {summary}\n")
                                fcntl.flock(ep, fcntl.LOCK_UN)
                        risks = reviewer_data.get("risk_areas", [])
                        if risks:
                            ui.log_history(f"Risk areas: {'; '.join(risks)}", "warn")
                    else:
                        reviewer_feedback = reviewer_data.get("feedback", "")
                    break
            else:
                # Reviewer failed to produce valid JSON 3 times — reject conservatively
                generation_approved = False
                reviewer_feedback = "Reviewer failed to produce valid output. Please review and retry."

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
        if total_gens % 3 == 0:
            ui.log_history("Consolidating experience pool...", "info")
            await _consolidate_experience_pool(ui, is_text_ui)

        # Generate evolution report every 5 generations
        if total_gens % 5 == 0:
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
