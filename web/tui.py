"""
Textual TUI for the Poker Bot Evolution Dashboard.

Located in web/ — integrates directly with web/core/evolution_core.py.
Design inspired by the React frontend (dark theme, card layout, pipeline badges).

Usage:
    python web/tui.py                    # Orchestrator mode (default)
    python web/tui.py --mode classic     # Classic evolution loop
    python web/tui.py --mode orchestrator --no-daemon
    python web/tui.py --workers 8 --pairs 3
"""

import sys
import os
import asyncio
import threading
import json
import time
from collections import deque
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = WEB_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
sys.path.insert(0, str(WEB_DIR / "core"))

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Header, Static, DataTable, RichLog, Footer

from rich.text import Text
from rich.panel import Panel
from rich.align import Align

from evolution_core import (
    BaseUI,
    main_loop,
    start_daemon,
    stop_daemon,
    daemon_monitor_thread,
    load_ratings,
    load_daemon_stats,
    Glicko2Player,
    daemon_proc,
    CORE_DIR,
    RESULTS_DIR,
    PROMPTS_DIR,
    PIPELINE_STATE_FILE,
    STAGE_ORDER,
    locked_file,
)


# ── Stage labels (Chinese, matching frontend) ──
STAGE_LABELS = {
    "prepared": "已准备",
    "workers_done": "工作器完成",
    "quality_passed": "质量通过",
    "reviewed": "已审核",
    "critic_checked": "策略审核",
}

# ── Icons for log_history statuses ──
STATUS_ICON = {
    "info": "🔹",
    "warn": "⚠️",
    "error": "❌",
    "success": "✨",
}
STATUS_COLOR = {
    "info": "cyan",
    "warn": "yellow",
    "error": "red",
    "success": "magenta",
}


class TuiApp(App, BaseUI):
    """Poker Bot Evolution Dashboard — Textual TUI (frontend-inspired)."""

    CSS_PATH = "tui.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("d", "toggle_dark", "Toggle Dark", show=False),
        Binding("up", "scroll_up", "↑ Scroll", show=True),
        Binding("down", "scroll_down", "↓ Scroll", show=True),
        Binding("left", "focus_left", "← Panel", show=True),
        Binding("right", "focus_right", "→ Panel", show=True),
        Binding("tab", "next_panel", "Next Panel", show=False),
        Binding("shift+tab", "prev_panel", "Prev Panel", show=False),
    ]

    # Focusable widgets for keyboard navigation
    PANEL_IDS = ["#stream-log", "#history-log", "#leaderboard-table"]

    # Reactive state (triggers watchers on change)
    header_text: reactive[str] = reactive("🔥 Poker Bot Evolution 🔥")
    status_msg: reactive[str] = reactive("Initializing...")
    is_working: reactive[bool] = reactive(False)

    # Runtime config (set before run())
    no_daemon: bool = False
    daemon_workers: int = 14
    daemon_pairs: int = 5
    mode: str = "orchestrator"  # "classic" or "orchestrator"

    def __init__(self):
        super().__init__()
        self._daemon_monitor_stop = None
        self._pipeline_timer = None

        # Cost tracking
        self.cost_log = deque(maxlen=20)
        self.gen_cost_total = 0.0
        self.grand_cost_total = 0.0

        # Sparkline cache
        self._sparkline_cache = ""
        self._sparkline_mtime = 0

        # Error buffer for debugging UI issues
        self._ui_error_log = deque(maxlen=50)

        # Tool call tracking (for card rendering in stream)
        self._open_tool_name = None
        self._open_tool_args = None

        # Worker tracking
        self._workers: dict[int, dict] = {}  # id -> {role, status}

        # Metrics cache
        self._metrics: dict = {}

    # ──────────────────────────────────────────────
    # Layout
    # ──────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(self.header_text, id="header-bar")

        with Horizontal(id="main-area"):
            # Left column: pipeline + stream + history
            with Vertical(id="left-col"):
                yield Static("Pipeline: —", id="pipeline-bar")
                yield RichLog(id="stream-log", highlight=True, markup=True,
                              auto_scroll=True)
                yield RichLog(id="history-log", highlight=True, markup=True,
                              auto_scroll=True)

            # Right column: stats + metrics + cost + workers + leaderboard + daemon
            with Vertical(id="right-col"):
                yield Static("Stats: —", id="stats-bar")
                yield Static("Metrics: —", id="metrics-widget")
                yield Static("Cost: —", id="cost-widget")
                yield Static("Workers: —", id="workers-widget")
                yield self._build_leaderboard()
                yield Static("Daemon: —", id="daemon-widget")

        yield Footer()

    def _build_leaderboard(self) -> DataTable:
        table = DataTable(id="leaderboard-table")
        table.show_cursor = False
        table.add_columns("#", "Bot", "Rating", "RD", "Conf")
        table.add_row("-", "Waiting...", "-", "-", "-")
        return table

    # ──────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────

    def on_mount(self) -> None:
        os.makedirs(PROMPTS_DIR, exist_ok=True)
        os.makedirs(RESULTS_DIR, exist_ok=True)

        # Start daemon
        if not self.no_daemon:
            start_daemon(workers=self.daemon_workers, pairs=self.daemon_pairs)
            self._daemon_monitor_stop = threading.Event()
            monitor = threading.Thread(
                target=daemon_monitor_thread,
                args=(self, self._daemon_monitor_stop),
                daemon=True,
            )
            monitor.start()

        # Pipeline refresh timer (every 3s)
        self._pipeline_timer = self.set_interval(3, self._refresh_pipeline)

        # Auto-focus stream log
        self.call_after_refresh(self._focus_stream)

        # Launch evolution loop
        if self.mode == "orchestrator":
            self.run_worker(self._run_orchestrator(), name="evolution", exclusive=True)
        else:
            self.run_worker(self._run_evolution(), name="evolution", exclusive=True)

    def _focus_stream(self):
        try:
            self.query_one("#stream-log").focus()
        except Exception as e:
            self._log_ui_error("_focus_stream", e)

    async def _run_evolution(self):
        try:
            await main_loop(self, is_text_ui=False, no_daemon=self.no_daemon)
        except Exception as e:
            self.log_history(f"Fatal error: {e}", "error")

    async def _run_orchestrator(self):
        try:
            from orchestrator import orchestrator_loop
            await orchestrator_loop(self, no_daemon=self.no_daemon)
        except Exception as e:
            self.log_history(f"Orchestrator fatal error: {e}", "error")

    def on_unmount(self) -> None:
        if self._pipeline_timer:
            self._pipeline_timer.stop()
        if self._daemon_monitor_stop:
            self._daemon_monitor_stop.set()
        if not self.no_daemon:
            stop_daemon()

    # ──────────────────────────────────────────────
    # Reactive watchers
    # ──────────────────────────────────────────────

    def watch_header_text(self, new_text: str) -> None:
        try:
            widget = self.query_one("#header-bar", Static)
            cost_str = f"  💰 ${self.grand_cost_total:.3f}" if self.grand_cost_total > 0 else ""
            status_icon = "🟢" if self.is_working else "⚪"
            widget.update(f"{status_icon} {new_text}{cost_str}")
        except Exception as e:
            self._log_ui_error("watch_header_text", e)

    def watch_status_msg(self, new_msg: str) -> None:
        try:
            self.watch_header_text(self.header_text)
        except Exception as e:
            self._log_ui_error("watch_status_msg", e)

    def watch_is_working(self, working: bool) -> None:
        self.watch_header_text(self.header_text)

    # ──────────────────────────────────────────────
    # BaseUI interface
    # ──────────────────────────────────────────────

    def log_history(self, msg, status="info"):
        icon = STATUS_ICON.get(status, "🔹")
        color = STATUS_COLOR.get(status, "cyan")
        try:
            log = self.query_one("#history-log", RichLog)
            log.write(f"[{color}]{icon} {msg}[/]")
        except Exception as e:
            self._log_ui_error("log_history", e)
        # Also parse worker status from history messages
        self._parse_worker_from_history(msg)

    def set_status(self, msg, is_working=False):
        self.status_msg = msg
        self.is_working = is_working

    def log_io(self, msg, stream_type="default"):
        color_map = {
            "prompt": "dim white",
            "claude": "bold bright_green",
            "thinking": "dim yellow",
            "tool": "dim cyan",
            "error": "bold red",
        }
        prefix_map = {
            "prompt": "│ ",
            "claude": "▸ ",
            "thinking": "… ",
            "tool": "⚙ ",
            "error": "✖ ",
        }
        color = color_map.get(stream_type, "white")
        prefix = prefix_map.get(stream_type, "  ")

        try:
            log = self.query_one("#stream-log", RichLog)

            # Tool output aggregation
            if stream_type == "tool" and self._open_tool_name:
                # Indent tool output under the open tool card
                for line in msg.split("\n"):
                    if line.strip():
                        log.write(f"[{color}]  │ {line}[/]")
                return

            for line in msg.split("\n"):
                if line.strip():
                    log.write(f"[{color}]{prefix}{line}[/]")
        except Exception as e:
            self._log_ui_error("log_io", e)

    def clear_io(self):
        try:
            log = self.query_one("#stream-log", RichLog)
            log.clear()
            self._open_tool_name = None
            self._open_tool_args = None
        except Exception as e:
            self._log_ui_error("clear_io", e)

    def update_eval_table(self, ratings, active_bots):
        try:
            table = self.query_one("#leaderboard-table", DataTable)
            table.clear()

            active_ratings = [(b, ratings.get(b, Glicko2Player())) for b in active_bots]
            active_ratings.sort(key=lambda x: x[1].r, reverse=True)

            for i, (bot, p) in enumerate(active_ratings):
                icon = "🏆" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else " "
                conf = self._confidence_bar(p.rd)
                table.add_row(
                    f"{icon}{i+1}",
                    bot.replace("claude_", "v"),
                    f"{p.r:.0f}",
                    f"±{p.rd:.0f}",
                    conf,
                )
        except Exception as e:
            self._log_ui_error("update_eval_table", e)

    def update_daemon_status(self, stats, ratings):
        total_matches = sum(stats.get("pairs", {}).values())
        periods = stats.get("total_periods", 0)
        n_bots = len(ratings)

        running = daemon_proc is not None and daemon_proc.poll() is None
        daemon_status = "🟢 运行中" if running else "🔴 已停止"
        pid_str = f"PID {daemon_proc.pid}" if running and daemon_proc else "N/A"

        avg_rd = ""
        if ratings:
            avg_rd = f"  Avg RD: {sum(p.rd for p in ratings.values()) / len(ratings):.0f}"

        lines = [
            f"[bold bright_cyan]⏱ 守护进程[/] {daemon_status} ({pid_str})",
            f"[bold]周期:[/] {periods}  [bold]对局:[/] {total_matches}  [bold]机器人:[/] {n_bots}",
            f"{avg_rd}",
        ]
        try:
            widget = self.query_one("#daemon-widget", Static)
            widget.update(Text.from_markup("\n".join(lines)))
            self._refresh_stats_bar(ratings, stats)
        except Exception as e:
            self._log_ui_error("update_daemon_status", e)

    def set_header(self, msg):
        self.header_text = msg

    def update_cost(self, role, cost_usd, usage):
        if cost_usd is not None:
            in_tok = usage.get("input_tokens", 0) if usage else 0
            out_tok = usage.get("output_tokens", 0) if usage else 0
            self.cost_log.append((role, cost_usd, in_tok, out_tok))
            self.gen_cost_total += cost_usd
            self.grand_cost_total += cost_usd
            self._refresh_cost_widget()
            self.watch_header_text(self.header_text)

    def update_metrics(self, metrics):
        self._metrics = metrics
        try:
            widget = self.query_one("#metrics-widget", Static)
            widget.update(self._build_metrics_text(metrics))
            self._refresh_stats_bar(load_ratings(), load_daemon_stats())
        except Exception as e:
            self._log_ui_error("update_metrics", e)

    def emit_tool_call(self, tool_name: str, args: dict):
        """Display a tool-call card header in the stream log."""
        self._open_tool_name = tool_name
        self._open_tool_args = args
        try:
            log = self.query_one("#stream-log", RichLog)
            args_summary = ""
            if args:
                # Show a compact summary of args
                try:
                    s = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
                    if len(s) > 80:
                        s = s[:77] + "..."
                    args_summary = f"  {s}"
                except Exception:
                    args_summary = "  {...}"
            log.write(f"[bold bright_cyan]╭─ ⚙ {tool_name}[/][dim cyan]{args_summary}[/]")
            log.write("[dim cyan]│[/]")
        except Exception as e:
            self._log_ui_error("emit_tool_call", e)

    def reset_gen_cost(self):
        self.gen_cost_total = 0.0
        self.cost_log.clear()
        self._refresh_cost_widget()

    # ──────────────────────────────────────────────
    # Panel refresh helpers
    # ──────────────────────────────────────────────

    def _refresh_stats_bar(self, ratings=None, stats=None):
        """Update the top stat-cards row (active bots, games, periods, top pair, daemon)."""
        try:
            widget = self.query_one("#stats-bar", Static)
        except Exception:
            return

        if ratings is None:
            ratings = load_ratings()
        if stats is None:
            stats = load_daemon_stats()

        n_bots = len(ratings)
        total_games = sum(stats.get("pairs", {}).values()) * 50
        periods = stats.get("total_periods", 0)
        pairs = stats.get("pairs", {})
        most_active = max(pairs.items(), key=lambda x: x[1]) if pairs else ("—", 0)

        running = daemon_proc is not None and daemon_proc.poll() is None
        daemon_emoji = "🟢" if running else "🔴"

        # Build a Rich Table with 5 columns (like frontend StatCards)
        table = Table.grid(padding=(0, 2))
        table.add_column(justify="center", min_width=10)
        table.add_column(justify="center", min_width=12)
        table.add_column(justify="center", min_width=10)
        table.add_column(justify="center", min_width=14)
        table.add_column(justify="center", min_width=12)

        table.add_row(
            f"[bold]{n_bots}[/]\n[dim]活跃机器人[/]",
            f"[bold]{total_games:,}[/]\n[dim]总对局[/]",
            f"[bold]{periods}[/]\n[dim]评分周期[/]",
            f"[bold]{most_active[0]}[/]\n[dim]{most_active[1]} 场[/]",
            f"[bold]{daemon_emoji}[/]\n[dim]守护进程[/]",
        )
        widget.update(table)

    def _build_metrics_text(self, metrics) -> Text:
        m = metrics
        total_s = int(m.get("total_time_s", 0))
        avg_s = m.get("avg_gen_time_s", 0)
        avg_m, avg_s_rem = divmod(int(avg_s), 60)
        sr = m.get("success_rate", 0)
        trend = m.get("rating_trend", 0)
        trend_icon = "▲" if trend > 0 else "▼" if trend < 0 else "▶"
        trend_color = "bright_green" if trend > 0 else "red" if trend < 0 else "yellow"
        sparkline = self._build_rating_sparkline()

        lines = [
            f"[bold bright_cyan]📊 进化指标[/]",
            f"  代次:    v{m.get('current_v', '?')} → v{m.get('next_v', '?')}",
            f"  总时间:  {total_s // 60}m {total_s % 60}s",
            f"  平均/代: {avg_m}m {avg_s_rem}s",
            f"  成功率:  {sr:.0%} ({m.get('total_success', 0)}/{m.get('total_gens', 0)})",
            f"  失败连击: {m.get('fail_count', 0)}",
            f"  评分趋势: [{trend_color}]{trend_icon} {trend:+.0f}[/]",
            f"  总成本:  ${self.grand_cost_total:.3f}",
        ]
        if sparkline:
            lines.append(f"  {sparkline}")
        return Text.from_markup("\n".join(lines))

    def _refresh_cost_widget(self):
        try:
            widget = self.query_one("#cost-widget", Static)
        except Exception:
            return

        if not self.cost_log and self.grand_cost_total == 0:
            widget.update("[dim]尚无成本数据[/]")
            return

        # Aggregate by role
        agg: dict[str, dict] = {}
        for role, cost, in_tok, out_tok in self.cost_log:
            if role not in agg:
                agg[role] = {"cost": 0.0, "in": 0, "out": 0}
            agg[role]["cost"] += cost
            agg[role]["in"] += in_tok
            agg[role]["out"] += out_tok

        lines = ["[bold bright_cyan]💰 成本分解[/]"]
        for role, d in agg.items():
            lines.append(
                f"  [dim]{role:12}[/] {d['in'] + d['out']:>8,} 令牌  ${d['cost']:.4f}"
            )
        lines.append(
            f"[dim]{'─' * 36}[/]\n"
            f"  [bold]本代 / 总计[/]    ${self.gen_cost_total:.3f} / ${self.grand_cost_total:.3f}"
        )
        widget.update(Text.from_markup("\n".join(lines)))

    def _refresh_workers_widget(self):
        try:
            widget = self.query_one("#workers-widget", Static)
        except Exception:
            return

        if not self._workers:
            widget.update("[dim]尚无工作器数据[/]")
            return

        lines = ["[bold bright_cyan]🔧 工作器进度[/]"]
        for wid, info in sorted(self._workers.items()):
            status = info.get("status", "unknown")
            role = info.get("role", "")
            role_str = f" ({role})" if role else ""
            icon = {"running": "●", "done": "✓", "failed": "✗"}.get(status, "?")
            color = {"running": "blue", "done": "green", "failed": "red"}.get(status, "white")
            lines.append(f"  [{color}]{icon}[/{color}] 工作器 {wid}{role_str} [{color}]{status}[/{color}]")
        widget.update(Text.from_markup("\n".join(lines)))

    def _refresh_pipeline(self):
        """Read pipeline checkpoint and update the pipeline bar."""
        try:
            if not PIPELINE_STATE_FILE.exists():
                try:
                    widget = self.query_one("#pipeline-bar", Static)
                    widget.update("Pipeline: [dim]无活跃代次[/]")
                except Exception:
                    pass
                return

            with locked_file(PIPELINE_STATE_FILE) as f:
                ckpt = json.load(f)
        except Exception:
            return

        next_v = ckpt.get("next_v", "?")
        source_v = ckpt.get("source_v", "?")
        stage = ckpt.get("stage", "unknown")
        attempt = ckpt.get("generation_attempt", 0) + 1
        current_idx = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else -1

        # Build colored stage badges
        badges = []
        for i, s in enumerate(STAGE_ORDER):
            label = STAGE_LABELS.get(s, s)
            if i < current_idx:
                badges.append(f"[bold green]✓ {label}[/]")
            elif i == current_idx:
                badges.append(f"[bold bright_blue underline]● {label}[/]")
            else:
                badges.append(f"[dim]○ {label}[/]")

        text = f"[bold]v{source_v} → v{next_v}[/]   {'  '.join(badges)}   [dim](尝试 {attempt})[/]"
        try:
            widget = self.query_one("#pipeline-bar", Static)
            widget.update(Text.from_markup(text))
        except Exception:
            pass

    def _build_rating_sparkline(self):
        """Build sparkline from rating_history.jsonl (top bot rating trend)."""
        history_file = RESULTS_DIR / "rating_history.jsonl"
        if not history_file.exists():
            return ""

        try:
            mt = os.path.getmtime(history_file)
            if mt == self._sparkline_mtime and self._sparkline_cache:
                return self._sparkline_cache

            snapshots = []
            with open(history_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    snap = json.loads(line)
                    ratings = snap.get("ratings", {})
                    if ratings:
                        top_r = max(p.get("r", 1500) for p in ratings.values())
                        snapshots.append(top_r)

            if len(snapshots) < 2:
                self._sparkline_cache = ""
                self._sparkline_mtime = mt
                return ""

            recent = snapshots[-20:]
            mn, mx = min(recent), max(recent)
            chars = "▁▂▃▄▅▆▇█"
            if mx == mn:
                result = f"趋势: {'█' * len(recent)} ({recent[-1]:.0f})"
            else:
                spark = ""
                for v in recent:
                    idx = int((v - mn) / (mx - mn) * (len(chars) - 1))
                    spark += chars[idx]
                result = f"趋势: [bright_cyan]{spark}[/] ({recent[0]:.0f}→{recent[-1]:.0f})"
            self._sparkline_cache = result
            self._sparkline_mtime = mt
            return result
        except Exception:
            return ""

    # ──────────────────────────────────────────────
    # Worker parsing
    # ──────────────────────────────────────────────

    def _parse_worker_from_history(self, msg: str):
        """Parse worker start/done/fail from history messages."""
        import re
        start_match = re.search(r'Worker[s]?\s+(\d+)(?:\s*\(([^)]+)\))?\s*(start|begin|running|launch)', msg, re.I)
        if start_match:
            wid = int(start_match.group(1))
            role = start_match.group(2) or ""
            self._workers[wid] = {"role": role, "status": "running"}
            self._refresh_workers_widget()
            return

        done_match = re.search(r'Worker[s]?\s+(\d+)(?:\s*\(([^)]+)\))?\s*(done|finish|success|complete)', msg, re.I)
        if done_match:
            wid = int(done_match.group(1))
            role = done_match.group(2) or self._workers.get(wid, {}).get("role", "")
            self._workers[wid] = {"role": role, "status": "done"}
            self._refresh_workers_widget()
            return

        fail_match = re.search(r'Worker[s]?\s+(\d+)(?:\s*\(([^)]+)\))?\s*(fail|error|timeout)', msg, re.I)
        if fail_match:
            wid = int(fail_match.group(1))
            role = fail_match.group(2) or self._workers.get(wid, {}).get("role", "")
            self._workers[wid] = {"role": role, "status": "failed"}
            self._refresh_workers_widget()
            return

    # ──────────────────────────────────────────────
    # Navigation actions
    # ──────────────────────────────────────────────

    def action_scroll_up(self):
        widget = self.focused
        if isinstance(widget, RichLog):
            widget.auto_scroll = False
            widget.scroll_relative(y=-3)

    def action_scroll_down(self):
        widget = self.focused
        if isinstance(widget, RichLog):
            widget.scroll_relative(y=3)
            if widget.is_vertical_scroll_end:
                widget.auto_scroll = True

    def _panel_index(self):
        for i, pid in enumerate(self.PANEL_IDS):
            try:
                if self.query_one(pid).has_focus:
                    return i
            except Exception:
                pass
        return 0

    def _focus_panel(self, index):
        pid = self.PANEL_IDS[index % len(self.PANEL_IDS)]
        try:
            self.query_one(pid).focus()
        except Exception:
            pass

    def action_focus_left(self):
        idx = self._panel_index()
        self._focus_panel(idx - 1)

    def action_focus_right(self):
        idx = self._panel_index()
        self._focus_panel(idx + 1)

    def action_next_panel(self):
        self.action_focus_right()

    def action_prev_panel(self):
        self.action_focus_left()

    # ──────────────────────────────────────────────
    # General helpers
    # ──────────────────────────────────────────────

    def _log_ui_error(self, context, exc):
        self._ui_error_log.append(f"{context}: {exc}")

    @staticmethod
    def _confidence_bar(rd):
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


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Poker Bot Evolution TUI")
    parser.add_argument("--mode", choices=["orchestrator", "classic"],
                        default="orchestrator", help="Evolution mode")
    parser.add_argument("--no-daemon", action="store_true",
                        help="No background evaluation daemon")
    parser.add_argument("--workers", type=int, default=14,
                        help="Daemon parallel workers")
    parser.add_argument("--pairs", type=int, default=5,
                        help="Mirror pairs per match")
    args = parser.parse_args()

    app = TuiApp()
    app.mode = args.mode
    app.no_daemon = args.no_daemon
    app.daemon_workers = args.workers
    app.daemon_pairs = args.pairs
    app.run()


if __name__ == "__main__":
    main()
