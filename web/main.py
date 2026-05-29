"""Unified web entry point for the Poker Evolution Framework.

Usage:
    python web/main.py                      # Default: orchestrator mode, auto-build frontend
    python web/main.py --mode classic       # Classic evolution loop
    python web/main.py --mode manual        # Manual mode (daemon only)
    python web/main.py --port 3000          # Custom port
    python web/main.py --no-daemon          # No background daemon
    python web/main.py --no-build           # Skip frontend build
    python web/main.py --tui                # Launch Textual TUI instead of web server
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
sys.path.insert(0, str(WEB_DIR / "core"))


def build_frontend() -> bool:
    """Build the frontend and copy dist to server/static. Returns True on success."""
    frontend_dir = WEB_DIR / "frontend"
    if not (frontend_dir / "package.json").exists():
        print("[build] package.json not found, skipping frontend build.", file=sys.stderr)
        return False

    print("[build] Building frontend...")
    result = subprocess.run(
        ["npm", "run", "build"],
        cwd=str(frontend_dir),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("[build] Frontend build failed:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        return False

    print("[build] Frontend build complete.")
    return True


def main():
    parser = argparse.ArgumentParser(description="Unified Evolution Web App")
    parser.add_argument("--mode", choices=["orchestrator", "classic", "manual"],
                        default=os.environ.get("EVOLUTION_MODE", "orchestrator"),
                        help="Evolution mode (default: orchestrator)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--no-daemon", action="store_true")
    parser.add_argument("--no-build", action="store_true", help="Skip frontend build on startup")
    parser.add_argument("--dev", action="store_true", help="Enable auto-reload")
    parser.add_argument("--tui", action="store_true",
                        help="Launch Textual TUI instead of web server")
    parser.add_argument("--workers", type=int, default=14,
                        help="Daemon parallel workers (TUI mode only)")
    parser.add_argument("--pairs", type=int, default=5,
                        help="Mirror pairs per match (TUI mode only)")
    args = parser.parse_args()

    # ── TUI mode ──
    if args.tui:
        if args.mode == "manual":
            print("[tui] Manual mode is not supported in TUI. Use 'classic' or 'orchestrator'.")
            sys.exit(1)
        from tui import TuiApp
        app = TuiApp()
        app.mode = args.mode
        app.no_daemon = args.no_daemon
        app.daemon_workers = args.workers
        app.daemon_pairs = args.pairs
        app.run()
        return

    # ── Web server mode ──
    import uvicorn

    os.environ["EVOLUTION_MODE"] = args.mode
    if args.no_daemon:
        os.environ["DAEMON_DISABLED"] = "1"

    # Auto-build frontend before starting server
    if not args.no_build:
        if not build_frontend():
            sys.exit(1)

    # Pre-populate app_state from CLI args so lifespan reads correct config
    sys.path.insert(0, str(WEB_DIR / "server"))
    from server.state import app_state
    app_state.update_config(mode=args.mode, daemon_enabled=not args.no_daemon)

    uvicorn.run(
        "server.app:app",
        host=args.host,
        port=args.port,
        reload=args.dev,
    )


if __name__ == "__main__":
    main()
