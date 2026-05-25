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
import asyncio
import fcntl
import atexit
import time
import threading
import random
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
from lineage import record_birth, get_stagnation_count, find_best_branch_source

# Global daemon process handle
daemon_proc = None


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
    if RATINGS_FILE.exists():
        with open(RATINGS_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
        return {name: Glicko2Player.from_dict(d) for name, d in data.items()}
    return {}


def load_daemon_stats():
    """Load daemon stats."""
    if STATS_FILE.exists():
        with open(STATS_FILE, "r") as f:
            return json.load(f)
    return {"pairs": {}, "total_periods": 0}


def wait_for_daemon_eval(bot_name, timeout=600, min_matches=20, max_rd=40):
    """Wait for daemon to evaluate a new bot.

    Requires both sufficient matches AND low rating deviation for confidence.
    """
    start = time.time()
    while time.time() - start < timeout:
        stats = load_daemon_stats()
        matches = sum(v for k, v in stats.get("pairs", {}).items() if bot_name in k)
        ratings = load_ratings()
        rd = ratings.get(bot_name, Glicko2Player()).rd
        if matches >= min_matches and rd <= max_rd:
            return True
        time.sleep(5)
    return False


# ──────────────────────────────────────────────
# Daemon Management
# ──────────────────────────────────────────────

def start_daemon(workers=14, pairs=5):
    """Start elo_daemon.py as a background subprocess."""
    global daemon_proc
    daemon_script = str(WORKSPACE / "elo_daemon.py")
    cmd = [sys.executable, daemon_script, "--workers", str(workers), "--pairs", str(pairs)]
    daemon_proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1
    )
    atexit.register(stop_daemon)
    return daemon_proc


def stop_daemon():
    """Stop the daemon subprocess."""
    global daemon_proc
    if daemon_proc and daemon_proc.poll() is None:
        daemon_proc.terminate()
        try:
            daemon_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon_proc.kill()
        daemon_proc = None


def daemon_monitor_thread(ui, stop_event):
    """Background thread that periodically reads daemon stats and updates UI."""
    while not stop_event.is_set():
        try:
            stats = load_daemon_stats()
            ratings = load_ratings()
            ui.update_daemon_status(stats, ratings)
        except Exception:
            pass
        stop_event.wait(3)


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

    try:
        async for message in claude_query(prompt=full_prompt, options=options):
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

    ui.update_cost(role_name, cost_usd, usage)

    return "\n".join(full_text), cost_usd, usage


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


def check_code_size(directory, max_lines_per_file=500):
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
    return []


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
            shutil.copytree(source_dir, target_dir)
            (target_dir / ".completed").touch()
            seeded = True
    return seeded


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
                            context_files, ui, is_text_ui, reviewer_feedback):
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
    src_dir = get_bot_dir(next_v - 1) if next_v > 1 else get_bot_dir(1)
    if next_dir.exists():
        shutil.rmtree(next_dir)
    shutil.copytree(src_dir, next_dir)

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
        # Seed lineage for initial bots
        for i in range(1, 7):
            bot_name = f"claude_v{i}"
            record_birth(bot_name, None, f"seeded from reference bot{i}")

    current_v = 1
    while True:
        target_dir = get_bot_dir(current_v)
        if target_dir.exists():
            if (target_dir / ".completed").exists():
                current_v += 1
            else:
                ui.log_history(f"Incomplete v{current_v} detected. Rolling back.", "warn")
                shutil.rmtree(target_dir)
                break
        else:
            break

    if current_v == 1:
        ui.log_history("No bots found. Initializing Genesis Bot (v1)...", "info")
        ui.set_status("Running Round 0 (Baseline Generation)...", is_working=True)
        os.makedirs(get_bot_dir(1), exist_ok=True)

        with open(PROMPTS_DIR / "initial_prompt.md") as f:
            prompt = f.read()
        instruction = prompt + "\n\nPlease write the full code for main.py, preflop.py, and postflop.py directly into bots/claude_v1/ directory."

        log_file = get_logs_dir(1) / "initial_generation_io.txt"

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
            ui.log_history("Genesis v1 generated successfully.", "success")
            break
        else:
            ui.log_history("Failed to generate Genesis bot. Exiting.", "error")
            return
    else:
        current_v -= 1
        ui.log_history(f"Resumed successfully from v{current_v}", "success")

    max_generations = current_v + 50
    ref_context = get_reference_context()
    ratings = load_ratings()

    while current_v < max_generations:
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

        # Stagnation detection & branch selection
        source_v = current_v
        stag = get_stagnation_count(f"claude_v{current_v}", ratings)
        if stag >= 2:
            branch_source = find_best_branch_source(
                active_bots, ratings, f"claude_v{current_v}"
            )
            if branch_source:
                source_v = int(branch_source.split("_v")[1])
                ui.log_history(
                    f"⚠️ Stagnation ({stag} gens). Branching from {branch_source} instead of v{current_v}",
                    "warn",
                )
                current_v = source_v

        ui.set_header(f"🔥 Antigravity Glicko-2 Evolution: v{source_v} ➡️ v{next_v} 🔥")

        # 1. Wait for daemon evaluation or run inline
        my_bot = f"claude_v{current_v}"

        if not no_daemon:
            ui.set_status(f"Waiting for daemon to evaluate v{current_v}...", is_working=True)
            ui.log_history(f"v{current_v} waiting for daemon evaluation...", "info")
            wait_for_daemon_eval(my_bot)
            ratings = load_ratings()
            ui.update_eval_table(ratings, active_bots)
        else:
            ui.set_status(f"v{current_v} inline evaluation...", is_working=True)
            ui.log_history(f"v{current_v} entering inline Glicko-2 evaluation...", "info")

            opponents_to_play = [b for b in active_bots if b != my_bot]
            if len(opponents_to_play) > 10:
                opponents_to_play = random.sample(opponents_to_play, 10)

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

        # 2. Master Analysis
        ui.set_status(f"Master Architect analyzing ecosystem...", is_working=True)
        with open(PROMPTS_DIR / "master_prompt.md") as f:
            master_prompt = f.read()

        my_p = ratings.get(my_bot, Glicko2Player())
        master_ctx = f"Current bot {my_bot} Glicko-2 Rating: r={my_p.r:.1f}, rd={my_p.rd:.1f}\n\n"

        active_ratings = [(b, ratings.get(b, Glicko2Player())) for b in active_bots]
        active_ratings.sort(key=lambda x: x[1].r, reverse=True)
        master_ctx += "Global Leaderboard (Top 3):\n"
        for i, (b, p) in enumerate(active_ratings[:3]):
            master_ctx += f"Rank {i+1}: {b} (Rating: {p.r:.1f}, RD: {p.rd:.1f})\n"

        # Rating trend — show recent generations
        master_ctx += "\nRecent Rating Trend:\n"
        for v in range(max(1, source_v - 3), source_v + 1):
            bot = f"claude_v{v}"
            if bot in ratings:
                p = ratings[bot]
                master_ctx += f"  {bot}: r={p.r:.1f} (RD: {p.rd:.1f})\n"

        context_files = [str(EXPERIENCE_FILE)]
        context_files += [str(get_bot_dir(current_v) / f) for f in os.listdir(get_bot_dir(current_v)) if f.endswith(".py")]

        if active_ratings:
            top_bot = active_ratings[0][0]
            top_dir = BOTS_DIR / top_bot
            for f in os.listdir(top_dir):
                if f.endswith(".py"):
                    context_files.append(str(top_dir / f))

        master_log_file = get_logs_dir(next_v) / "master_io.txt"

        tasks_data = None
        for attempt in range(3):
            ui.clear_io()
            master_output, _, _ = await run_claude_query(master_prompt + "\n" + master_ctx, context_files, ui, f"MASTER (Try {attempt+1})", master_log_file, is_text_ui)
            tasks_data = parse_json_output(master_output)
            if tasks_data and "tasks" in tasks_data:
                ui.log_history("Master analysis complete. Blueprint designed.", "success")
                if "new_experience" in tasks_data:
                    with open(EXPERIENCE_FILE, "a") as ef:
                        ef.write(f"\n- **v{current_v} -> v{next_v}**: {tasks_data['new_experience']}")
                break
            else:
                ui.log_history("Master output malformed JSON. Retrying...", "warn")
                await asyncio.sleep(2)
        else:
            ui.log_history("Master failed to plan. Halting.", "error")
            break

        generation_approved = False
        reviewer_feedback = ""

        for generation_attempt in range(3):
            if generation_approved:
                break

            ui.log_history(f"Generation Pipeline (Attempt {generation_attempt+1})", "info")

            next_dir = get_bot_dir(next_v)
            if next_dir.exists():
                shutil.rmtree(next_dir)
            shutil.copytree(get_bot_dir(current_v), next_dir)

            with open(PROMPTS_DIR / "worker_prompt.md") as f:
                worker_template = f.read()

            # Build worker context files (shared across all workers)
            worker_context_files = [str(next_dir / f) for f in os.listdir(next_dir) if f.endswith(".py")]
            if active_ratings:
                top_bot = active_ratings[0][0]
                top_dir = BOTS_DIR / top_bot
                for f in os.listdir(top_dir):
                    if f.endswith(".py"):
                        worker_context_files.append(str(top_dir / f))

            workers_succeeded = await _execute_workers(
                tasks_data["tasks"], worker_template, next_dir, next_v,
                worker_context_files, ui, is_text_ui, reviewer_feedback,
            )

            if not workers_succeeded:
                break

            # Single-file size constraint — ensure readability
            total_lines, oversized_files = check_code_size(next_dir)
            if oversized_files:
                details = ", ".join(f"{name}={lines}" for name, lines in oversized_files)
                reviewer_feedback = (
                    f"These files exceed 500 lines and must be split into modules: {details}. "
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

            final_context_files = [str(next_dir / f) for f in os.listdir(next_dir) if f.endswith(".py")]

            for review_attempt in range(3):
                ui.clear_io()
                reviewer_output, _, _ = await run_claude_query(reviewer_prompt, final_context_files, ui, "LEAD CODE REVIEWER", reviewer_log_file, is_text_ui)
                reviewer_data = parse_json_output(reviewer_output)

                if reviewer_data and "approved" in reviewer_data:
                    if reviewer_data["approved"]:
                        generation_approved = True
                    else:
                        reviewer_feedback = reviewer_data.get("feedback", "")
                    break
            else:
                generation_approved = True

        if not generation_approved:
            break

        (next_dir / ".completed").touch()

        # Record lineage
        source_bot = f"claude_v{source_v}"
        next_bot = f"claude_v{next_v}"
        strategy_tag = tasks_data.get("analysis", "")[:80] if tasks_data.get("analysis") else ""
        record_birth(next_bot, source_bot, strategy_tag)

        current_v = next_v
        ui.log_history(f"Successfully evolved to v{current_v}! 🎉", "success")
        if hasattr(ui, 'reset_gen_cost'):
            ui.reset_gen_cost()

    ui.set_status("Evolution Complete.", is_working=False)
    ui.log_history("Matrix simulation concluded.", "success")
    await asyncio.sleep(5)
