"""
Textual TUI for the Poker Bot Evolution Dashboard.

Beautiful dark-themed dashboard using the Textual framework.
Implements the BaseUI interface from evolution_core.
"""

import threading
from collections import deque
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Header, Static, DataTable, RichLog, Footer
from textual.widgets._data_table import CellDoesNotExist

from rich.text import Text
from rich.table import Table

from evolution_core import (
    BaseUI, main_loop, start_daemon, stop_daemon,
    daemon_monitor_thread, load_ratings, Glicko2Player,
    WORKSPACE, RESULTS_DIR, PROMPTS_DIR,
)


class EvolutionApp(App, BaseUI):
    """Poker Bot Evolution Dashboard — Textual TUI."""

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

    # Focusable panels for left/right/tab navigation
    PANEL_IDS = ["#stream-log", "#history-log", "#leaderboard-table", "#cost-widget"]

    # Reactive state
    header_text: reactive[str] = reactive("🔥 Antigravity Glicko-2 Poker Evolution 🔥")
    status_msg: reactive[str] = reactive("Initializing...")
    is_working: reactive[bool] = reactive(False)

    # Daemon config (set before run())
    no_daemon: bool = False
    daemon_workers: int = 14
    daemon_pairs: int = 5

    def __init__(self):
        super().__init__()
        self._daemon_monitor_stop = None
        # Cost tracking
        self.cost_log = deque(maxlen=20)
        self.gen_cost_total = 0.0
        self.grand_cost_total = 0.0

    # ──────────────────────────────────────────────
    # Layout
    # ──────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        # Header bar
        yield Static(self.header_text, id="header-bar")

        with Horizontal(id="main-area"):
            # Left column: status + daemon + history
            with Vertical(id="left-col"):
                with Vertical(id="status-panel"):
                    yield Static("Initializing...", id="status-widget", classes="panel")
                with Vertical(id="daemon-panel"):
                    yield Static("Daemon: waiting...", id="daemon-widget", classes="panel")
                with Vertical(id="history-panel"):
                    yield RichLog(id="history-log", highlight=True, markup=True, auto_scroll=True, classes="panel")

            # Right column: leaderboard + cost
            with Vertical(id="right-col"):
                with Vertical(id="leaderboard-panel"):
                    yield self._build_leaderboard()
                with Vertical(id="cost-panel"):
                    yield Static("No costs yet", id="cost-widget", classes="panel")

        # LLM Stream (full width bottom)
        with Vertical(id="stream-panel"):
            yield RichLog(id="stream-log", highlight=True, markup=True, auto_scroll=True, classes="panel")

        yield Footer()

    def _build_leaderboard(self) -> DataTable:
        table = DataTable(id="leaderboard-table", classes="panel")
        table.show_cursor = False
        table.add_columns("#", "Bot", "Rating", "RD", "Confidence")
        table.add_row("-", "Waiting...", "-", "-", "-")
        return table

    # ──────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────

    def on_mount(self) -> None:
        import os
        os.makedirs(PROMPTS_DIR, exist_ok=True)
        os.makedirs(RESULTS_DIR, exist_ok=True)

        # Start daemon
        if not self.no_daemon:
            proc = start_daemon(workers=self.daemon_workers, pairs=self.daemon_pairs)
            self._daemon_monitor_stop = threading.Event()
            monitor = threading.Thread(
                target=daemon_monitor_thread,
                args=(self, self._daemon_monitor_stop),
                daemon=True,
            )
            monitor.start()

        # Auto-focus stream log so arrow keys work immediately
        self.call_after_refresh(self._focus_stream)

        # Start evolution loop as async worker
        self.run_worker(self._run_evolution(), name="evolution", exclusive=True)

    def _focus_stream(self):
        try:
            self.query_one("#stream-log").focus()
        except Exception:
            pass

    async def _run_evolution(self):
        try:
            await main_loop(self, is_text_ui=False, no_daemon=self.no_daemon)
        except Exception as e:
            self.log_history(f"Fatal error: {e}", "error")

    def on_unmount(self) -> None:
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
            widget.update(new_text + cost_str)
        except Exception:
            pass

    def watch_status_msg(self, new_msg: str) -> None:
        try:
            widget = self.query_one("#status-widget", Static)
            if self.is_working:
                widget.update(f"● {new_msg}")
            else:
                widget.update(f"✅ {new_msg}")
        except Exception:
            pass

    def watch_is_working(self, working: bool) -> None:
        # Re-trigger status display
        self.watch_status_msg(self.status_msg)

    # ──────────────────────────────────────────────
    # BaseUI interface
    # ──────────────────────────────────────────────

    def log_history(self, msg, status="info"):
        icon = {"info": "🔹", "warn": "⚠️", "error": "❌", "success": "✨"}.get(status, "🔹")
        color = {"info": "cyan", "warn": "yellow", "error": "red", "success": "magenta"}.get(status, "cyan")
        try:
            log = self.query_one("#history-log", RichLog)
            log.write(f"[{color}]{icon} {msg}[/]")
        except Exception:
            pass

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
            for line in msg.split("\n"):
                log.write(f"[{color}]{prefix}{line}[/]")
        except Exception:
            pass

    def clear_io(self):
        try:
            log = self.query_one("#stream-log", RichLog)
            log.clear()
        except Exception:
            pass

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
                    bot.replace("claude_", ""),
                    f"{p.r:.0f}",
                    f"±{p.rd:.0f}",
                    conf,
                )
        except Exception:
            pass

    def update_daemon_status(self, stats, ratings):
        from evolution_core import daemon_proc

        total_matches = sum(stats.get("pairs", {}).values())
        periods = stats.get("total_periods", 0)
        n_bots = len(ratings)

        daemon_status = "🟢 Running" if (daemon_proc and daemon_proc.poll() is None) else "🔴 Stopped"
        pid_str = f"PID {daemon_proc.pid}" if (daemon_proc and daemon_proc.poll() is None) else "N/A"

        avg_rd = f"  Avg RD: {sum(p.rd for p in ratings.values()) / len(ratings):.0f}" if ratings else ""

        lines = [
            f"[bold bright_cyan]⏱ Daemon:[/] {daemon_status} ({pid_str})",
            f"[bold]Periods:[/] {periods}  [bold]Games:[/] {total_matches}  [bold]Bots:[/] {n_bots}",
            f"{avg_rd}",
        ]
        try:
            widget = self.query_one("#daemon-widget", Static)
            widget.update(Text.from_markup("\n".join(lines)))
        except Exception:
            pass

    def set_header(self, msg):
        self.header_text = msg

    def update_cost(self, role, cost_usd, usage):
        if cost_usd is not None:
            in_tok = usage.get("input_tokens", 0) if usage else 0
            out_tok = usage.get("output_tokens", 0) if usage else 0
            self.cost_log.append((role, cost_usd, in_tok, out_tok))
            self.gen_cost_total += cost_usd
            self.grand_cost_total += cost_usd
            self._refresh_cost_panel()

    def reset_gen_cost(self):
        self.gen_cost_total = 0.0
        self._refresh_cost_panel()

    def _refresh_cost_panel(self):
        lines = []
        for role, cost, in_tok, out_tok in self.cost_log:
            r = role[:18]
            lines.append(f"[bright_yellow]  {r:<18}[/] ${cost:.4f}  ({in_tok//1000}K→{out_tok//1000}K)")
        lines.append(f"\n[bold bright_white]  {'Gen Total:':<18}[/] ${self.gen_cost_total:.4f}")
        lines.append(f"[bold bright_green]  {'Grand Total:':<18}[/] ${self.grand_cost_total:.4f}")
        try:
            widget = self.query_one("#cost-widget", Static)
            widget.update(Text.from_markup("\n".join(lines)))
        except Exception:
            pass
        # Also update header cost display
        self.watch_header_text(self.header_text)

    # ──────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────

    # ── Arrow key actions ──

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

    # ── General helpers ──

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


if __name__ == "__main__":
    app = EvolutionApp()
    app.run()
