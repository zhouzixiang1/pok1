import os
import sys
import json
import shutil
import subprocess
import re
import argparse
import asyncio
import fcntl
import atexit
import signal
from pathlib import Path
from collections import deque
from threading import Event
import threading
import time

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

# Global daemon process handle
daemon_proc = None


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


def wait_for_daemon_eval(bot_name, timeout=600):
    """Wait for daemon to evaluate a new bot (at least 10 matches)."""
    start = time.time()
    while time.time() - start < timeout:
        stats = load_daemon_stats()
        matches = sum(v for k, v in stats.get("pairs", {}).items() if bot_name in k)
        if matches >= 10:
            return True
        time.sleep(5)
    return False


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


# ──────────────────────────────────────────────
# UI Classes
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


class EvolutionUI(BaseUI):
    def __init__(self):
        from rich.layout import Layout
        from rich.panel import Panel
        from rich.text import Text
        from rich.table import Table
        from rich.spinner import Spinner
        from rich.progress_bar import ProgressBar

        self.Panel = Panel
        self.Text = Text
        self.Table = Table
        self.Spinner = Spinner

        # Layout: header + main (left+right) + bottom stream
        self.layout = Layout()
        self.layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main", ratio=3),
            Layout(name="stream", ratio=2)
        )
        self.layout["main"].split_row(
            Layout(name="left", ratio=2),
            Layout(name="right", ratio=3)
        )
        self.layout["left"].split_column(
            Layout(name="status", size=3),
            Layout(name="daemon_info", size=6),
            Layout(name="history", ratio=1)
        )
        self.layout["right"].split_column(
            Layout(name="leaderboard", ratio=3),
            Layout(name="cost_panel", ratio=1),
            Layout(name="match_feed", ratio=1)
        )

        # State
        self.history_log = deque(maxlen=20)
        self.io_log = deque(maxlen=60)
        self.match_feed = deque(maxlen=6)
        self.cost_log = deque(maxlen=20)  # (role, cost, in_tokens, out_tokens)
        self.gen_cost_total = 0.0
        self.grand_cost_total = 0.0
        self.status_msg = "Initializing..."
        self.is_working = False
        self.header_msg = "🔥 Antigravity Glicko-2 Poker Evolution 🔥"
        self.daemon_info_text = self.Text("Daemon: waiting...")
        self.eval_table = self.build_empty_table()

        self.update_layout()

    def build_empty_table(self):
        table = self.Table(title="Glicko-2 Leaderboard", expand=True, border_style="bright_blue")
        table.add_column("#", justify="center", style="cyan", width=3)
        table.add_column("Bot", justify="left", style="magenta")
        table.add_column("Rating", justify="right", style="green")
        table.add_column("RD", justify="right")
        table.add_column("Confidence", justify="left")
        table.add_row("-", "Waiting...", "-", "-", "-")
        return table

    def _confidence_bar(self, rd):
        """Visual confidence indicator based on RD."""
        # RD < 50: very confident, RD > 200: very uncertain
        pct = max(0, min(100, 100 - rd / 3.5))
        filled = int(pct / 10)
        bar = "█" * filled + "░" * (10 - filled)
        if rd < 50:
            color = "bright_green"
        elif rd < 100:
            color = "green"
        elif rd < 200:
            color = "yellow"
        else:
            color = "red"
        return f"[{color}]{bar}[/{color}] {pct:.0f}%"

    def update_eval_table(self, ratings, active_bots):
        table = self.Table(
            title=f"[bold]Glicko-2 Leaderboard[/]  [dim]({len(active_bots)} bots)[/]",
            expand=True, border_style="bright_blue", show_lines=False
        )
        table.add_column("#", justify="center", style="cyan", width=3, no_wrap=True)
        table.add_column("Bot", style="magenta", no_wrap=False)
        table.add_column("Rating", justify="right", style="bold green")
        table.add_column("RD", justify="right", style="dim")
        table.add_column("Confidence", justify="left", no_wrap=True)

        active_ratings = [(b, ratings.get(b, Glicko2Player())) for b in active_bots]
        active_ratings.sort(key=lambda x: x[1].r, reverse=True)

        for i, (bot, p) in enumerate(active_ratings):
            icon = "🏆" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else " "
            conf = self._confidence_bar(p.rd)
            table.add_row(
                f"{icon}{i+1}",
                bot.replace("claude_", ""),
                f"{p.r:.0f}",
                f"±{p.rd:.0f}",
                conf
            )

        self.eval_table = table
        self.update_layout()

    def update_daemon_status(self, stats, ratings):
        """Update the daemon info panel."""
        total_matches = sum(stats.get("pairs", {}).values())
        periods = stats.get("total_periods", 0)
        n_bots = len(ratings)

        # Find highest RD bot (most uncertain)
        if ratings:
            most_uncertain = max(ratings.items(), key=lambda x: x[1].rd)
            uncertain_str = f"  Most uncertain: {most_uncertain[0].replace('claude_', '')} (RD={most_uncertain[1].rd:.0f})"
        else:
            uncertain_str = ""

        # Average RD
        if ratings:
            avg_rd = sum(p.rd for p in ratings.values()) / len(ratings)
            avg_str = f"  Avg RD: {avg_rd:.0f}"
        else:
            avg_str = ""

        daemon_status = "🟢 Running" if (daemon_proc and daemon_proc.poll() is None) else "🔴 Stopped"
        pid_str = f"PID {daemon_proc.pid}" if (daemon_proc and daemon_proc.poll() is None) else "N/A"

        lines = [
            f"[bold bright_cyan]⏱ Daemon Status:[/]  {daemon_status}  ({pid_str})",
            f"[bold]Periods:[/] {periods}    [bold]Total games:[/] {total_matches}    [bold]Bots tracked:[/] {n_bots}",
            f"{avg_str}",
            f"{uncertain_str}",
        ]
        text = self.Text.from_markup("\n".join(lines))
        self.daemon_info_text = text
        self.update_layout()

    def add_match_result(self, bot_a, bot_b, score_a, score_b):
        """Add a match result to the live feed."""
        self.match_feed.append((bot_a, bot_b, score_a, score_b))
        self.update_layout()

    def set_header(self, msg):
        self.header_msg = msg
        self.update_layout()

    def update_cost(self, role, cost_usd, usage):
        """Track per-agent cost and update display."""
        if cost_usd is not None:
            in_tok = usage.get("input_tokens", 0) if usage else 0
            out_tok = usage.get("output_tokens", 0) if usage else 0
            self.cost_log.append((role, cost_usd, in_tok, out_tok))
            self.gen_cost_total += cost_usd
            self.grand_cost_total += cost_usd
            self.update_layout()

    def reset_gen_cost(self):
        """Reset per-generation cost tracker."""
        self.gen_cost_total = 0.0

    def update_layout(self):
        from rich.console import Group
        from rich.text import Text

        # Header
        cost_str = f"  💰 ${self.grand_cost_total:.3f}" if self.grand_cost_total > 0 else ""
        self.layout["header"].update(self.Panel(
            Text(self.header_msg + cost_str, style="bold white on deep_sky_blue1", justify="center"),
            border_style="deep_sky_blue1"
        ))

        # Status
        if self.is_working:
            status_render = self.Spinner("dots", text=Text(self.status_msg, style="bold bright_green"))
        else:
            status_render = Text(f"✅ {self.status_msg}", style="bold green")
        self.layout["status"].update(self.Panel(
            status_render,
            title="[bold bright_yellow]System Status[/]",
            border_style="yellow", padding=(0, 1)
        ))

        # Daemon info
        self.layout["daemon_info"].update(self.Panel(
            self.daemon_info_text,
            title="[bold bright_blue]Daemon Monitor[/]",
            border_style="blue", padding=(0, 1)
        ))

        # History
        history_text = Text()
        for msg, status in self.history_log:
            icon = "🔹" if status == "info" else "⚠️" if status == "warn" else "❌" if status == "error" else "✨"
            color = "cyan" if status == "info" else "yellow" if status == "warn" else "red" if status == "error" else "magenta"
            history_text.append(f"{icon} {msg}\n", style=color)
        self.layout["history"].update(self.Panel(
            history_text,
            title="[bold bright_cyan]Evolution Log[/]",
            border_style="cyan"
        ))

        # Leaderboard
        self.layout["leaderboard"].update(self.Panel(
            self.eval_table,
            border_style="bright_blue"
        ))

        # Match feed
        feed_text = Text()
        for bot_a, bot_b, sa, sb in self.match_feed:
            a = bot_a.replace("claude_", "")
            b = bot_b.replace("claude_", "")
            if sa > sb:
                feed_text.append(f"✅ {a} beat {b} ({sa}-{sb})\n", style="bright_green")
            elif sb > sa:
                feed_text.append(f"❌ {a} lost to {b} ({sa}-{sb})\n", style="bright_red")
            else:
                feed_text.append(f"➖ {a} drew {b} ({sa}-{sb})\n", style="yellow")
        self.layout["match_feed"].update(self.Panel(
            feed_text or Text("  No matches yet...", style="dim"),
            title="[bold bright_white]Recent Matches[/]",
            border_style="white"
        ))

        # Cost panel
        cost_text = Text()
        for role, cost, in_tok, out_tok in self.cost_log:
            r = role[:18]
            cost_text.append(f"  {r:<18} ${cost:.4f}  ({in_tok//1000}K→{out_tok//1000}K)\n", style="bright_yellow")
        cost_text.append(f"\n  {'Gen Total:':<18} ${self.gen_cost_total:.4f}\n", style="bold bright_white")
        cost_text.append(f"  {'Grand Total:':<18} ${self.grand_cost_total:.4f}", style="bold bright_green")
        self.layout["cost_panel"].update(self.Panel(
            cost_text,
            title="[bold bright_yellow]💰 Cost Tracker[/]",
            border_style="yellow"
        ))

        # LLM Stream
        stream_render = Text()
        for line, stream_type in self.io_log:
            if stream_type == "prompt":
                stream_render.append(line + "\n", style="dim white")
            elif stream_type == "claude":
                stream_render.append(line + "\n", style="bold bright_green")
            elif stream_type == "thinking":
                stream_render.append(line + "\n", style="dim yellow")
            elif stream_type == "tool":
                stream_render.append(line + "\n", style="dim cyan")
            elif stream_type == "error":
                stream_render.append(line + "\n", style="bold red")
            else:
                stream_render.append(line + "\n", style="white")
        self.layout["stream"].update(self.Panel(
            stream_render,
            title="[bold magenta]🧠 LLM Stream[/]",
            border_style="magenta"
        ))

    def log_history(self, msg, status="info"):
        self.history_log.append((msg, status))
        self.update_layout()

    def set_status(self, msg, is_working=False):
        self.status_msg = msg
        self.is_working = is_working
        self.update_layout()

    def log_io(self, msg, stream_type="default"):
        for line in msg.split("\n"):
            self.io_log.append((line, stream_type))
        self.update_layout()

    def clear_io(self):
        self.io_log.clear()
        self.update_layout()


# ──────────────────────────────────────────────
# Core functions
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

    # Report cost
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
# Main loop
# ──────────────────────────────────────────────

async def main_loop(ui, is_text_ui, no_daemon=False):
    os.makedirs(GRAVEYARD_DIR, exist_ok=True)

    if seed_initial_bots(ui):
        ui.log_history("Bootstrap complete: v1 to v6 initialized.", "success")

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
        ui.set_header(f"🔥 Antigravity Glicko-2 Evolution: v{current_v} ➡️ v{next_v} 🔥")

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

            import random
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

            workers_succeeded = True
            for i, task in enumerate(tasks_data["tasks"]):
                w_id = task.get("worker_id", i+1)
                role = task.get("role", f"Expert Coder {w_id}")
                base_worker_prompt = task.get("worker_prompt", task.get("instruction", ""))

                if reviewer_feedback:
                    base_worker_prompt = f"CRITICAL REVISION NEEDED:\n{reviewer_feedback}\n\nORIGINAL:\n{base_worker_prompt}"

                worker_log_file = get_logs_dir(next_v) / f"worker_{w_id}_io.txt"

                for attempt in range(4):
                    ui.clear_io()
                    ui.set_status(f"[{role}] coding for v{next_v}...", is_working=True)

                    worker_prompt = worker_template.replace("{role}", role).replace("{worker_prompt}", base_worker_prompt).replace("{version}", str(next_v))

                    new_context_files = [str(next_dir / f) for f in os.listdir(next_dir) if f.endswith(".py")]
                    if active_ratings:
                        top_bot = active_ratings[0][0]
                        top_dir = BOTS_DIR / top_bot
                        for f in os.listdir(top_dir):
                            if f.endswith(".py"):
                                new_context_files.append(str(top_dir / f))

                    await run_claude_query(worker_prompt, new_context_files, ui, f"WORKER {w_id} ({role})", worker_log_file, is_text_ui)

                    compile_errors = verify_code(next_dir)
                    if compile_errors:
                        base_worker_prompt += f"\n\nCRITICAL FIX: Fix syntax error:\n{compile_errors[0]}"
                        continue

                    smoke_errors = run_smoke_test(next_dir)
                    if smoke_errors:
                        base_worker_prompt += f"\n\nCRITICAL FIX: Fix runtime error:\n{smoke_errors[0]}"
                        continue

                    break
                else:
                    workers_succeeded = False
                    break

            if not workers_succeeded:
                break

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
        current_v = next_v
        ui.log_history(f"Successfully evolved to v{current_v}! 🎉", "success")
        if hasattr(ui, 'reset_gen_cost'):
            ui.reset_gen_cost()

    ui.set_status("Evolution Complete.", is_working=False)
    ui.log_history("Matrix simulation concluded.", "success")
    await asyncio.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="Antigravity Glicko-2 Poker Evolution Framework")
    parser.add_argument("--no-tui", action="store_true", help="Run in text-only mode.")
    parser.add_argument("--no-daemon", action="store_true", help="Inline evaluation, no background daemon.")
    parser.add_argument("--workers", type=int, default=14, help="Daemon parallel workers.")
    parser.add_argument("--pairs", type=int, default=5, help="Mirror pairs per match.")
    args = parser.parse_args()

    os.makedirs(PROMPTS_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)

    # Start daemon subprocess (unless --no-daemon)
    daemon_monitor_stop = None
    if not args.no_daemon:
        proc = start_daemon(workers=args.workers, pairs=args.pairs)
        print(f"[MAIN] Daemon started (PID {proc.pid})")

    if args.no_tui:
        ui = TextUI()
        try:
            asyncio.run(main_loop(ui, is_text_ui=True, no_daemon=args.no_daemon))
        finally:
            if not args.no_daemon:
                stop_daemon()
                print("[MAIN] Daemon stopped.")
    else:
        from rich.live import Live
        ui = EvolutionUI()

        # Start daemon monitor thread
        if not args.no_daemon:
            daemon_monitor_stop = threading.Event()
            monitor = threading.Thread(target=daemon_monitor_thread, args=(ui, daemon_monitor_stop), daemon=True)
            monitor.start()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            with Live(ui.layout, refresh_per_second=15, screen=True) as live:
                original_update = ui.update_layout
                def new_update():
                    original_update()
                    live.refresh()
                ui.update_layout = new_update
                loop.run_until_complete(main_loop(ui, is_text_ui=False, no_daemon=args.no_daemon))
        finally:
            loop.close()
            if daemon_monitor_stop:
                daemon_monitor_stop.set()
            if not args.no_daemon:
                stop_daemon()


if __name__ == "__main__":
    main()
