"""
Entry point for the Antigravity Poker Bot Evolution Framework.

Thin launcher that delegates to:
  - evolution_core.py: core business logic (main_loop, LLM orchestration, ratings)
  - tui.py: Textual TUI with streaming display and arrow key navigation
"""

import os
import sys
import asyncio
import argparse
import threading
from pathlib import Path

WORKSPACE = Path("evolution_workspace")
sys.path.insert(0, str(WORKSPACE.resolve()))

from evolution_core import (
    main_loop, TextUI,
    start_daemon, stop_daemon,
    daemon_monitor_thread,
    PROMPTS_DIR, RESULTS_DIR,
)


def main():
    parser = argparse.ArgumentParser(
        description="Antigravity Glicko-2 Poker Evolution Framework"
    )
    parser.add_argument("--no-tui", action="store_true",
                        help="Run in text-only mode (no Textual TUI).")
    parser.add_argument("--no-daemon", action="store_true",
                        help="Inline evaluation, no background daemon.")
    parser.add_argument("--workers", type=int, default=14,
                        help="Daemon parallel workers.")
    parser.add_argument("--pairs", type=int, default=5,
                        help="Mirror pairs per match.")
    args = parser.parse_args()

    os.makedirs(PROMPTS_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)

    if args.no_tui:
        # ── Text-only mode ──
        ui = TextUI()
        daemon_monitor_stop = None

        if not args.no_daemon:
            start_daemon(workers=args.workers, pairs=args.pairs)
            daemon_monitor_stop = threading.Event()
            monitor = threading.Thread(
                target=daemon_monitor_thread,
                args=(ui, daemon_monitor_stop),
                daemon=True,
            )
            monitor.start()

        try:
            asyncio.run(main_loop(ui, is_text_ui=True, no_daemon=args.no_daemon))
        except KeyboardInterrupt:
            print("\n[INTERRUPTED] Shutting down...")
        finally:
            if daemon_monitor_stop:
                daemon_monitor_stop.set()
            if not args.no_daemon:
                stop_daemon()
    else:
        # ── Textual TUI mode (default) ──
        from tui import EvolutionApp

        app = EvolutionApp()
        app.no_daemon = args.no_daemon
        app.daemon_workers = args.workers
        app.daemon_pairs = args.pairs
        app.run()


if __name__ == "__main__":
    main()
