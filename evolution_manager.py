"""
Entry point for the Antigravity Glicko-2 Poker Bot Evolution Framework.

Usage:
    python evolution_workspace/evolution_manager.py                       # Textual TUI mode
    python evolution_workspace/evolution_manager.py --no-tui              # Plain text mode
    python evolution_workspace/evolution_manager.py --no-daemon           # Inline eval (no background daemon)
    python evolution_workspace/evolution_manager.py --workers 8 --pairs 3 # Custom daemon settings
"""

import sys
import argparse
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKSPACE))


def main():
    parser = argparse.ArgumentParser(description="Antigravity Glicko-2 Poker Evolution Framework")
    parser.add_argument("--no-tui", action="store_true", help="Run in text-only mode.")
    parser.add_argument("--no-daemon", action="store_true", help="Inline evaluation, no background daemon.")
    parser.add_argument("--workers", type=int, default=14, help="Daemon parallel workers.")
    parser.add_argument("--pairs", type=int, default=5, help="Mirror pairs per match.")
    args = parser.parse_args()

    if args.no_tui:
        import asyncio
        from evolution_core import main_loop, TextUI, stop_daemon

        ui = TextUI()
        try:
            asyncio.run(main_loop(ui, is_text_ui=True, no_daemon=args.no_daemon))
        finally:
            if not args.no_daemon:
                stop_daemon()
    else:
        from tui import EvolutionApp

        app = EvolutionApp()
        app.no_daemon = args.no_daemon
        app.daemon_workers = args.workers
        app.daemon_pairs = args.pairs
        app.run()


if __name__ == "__main__":
    main()
