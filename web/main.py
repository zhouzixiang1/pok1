"""Unified web entry point for the Poker Evolution Framework.

Usage:
    python web/main.py                      # Default: orchestrator mode
    python web/main.py --mode classic       # Classic evolution loop
    python web/main.py --mode manual        # Manual mode (daemon only)
    python web/main.py --port 3000          # Custom port
    python web/main.py --no-daemon          # No background daemon
"""

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
sys.path.insert(0, str(WEB_DIR / "core"))

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Unified Evolution Web App")
    parser.add_argument("--mode", choices=["orchestrator", "classic", "manual"],
                        default=os.environ.get("EVOLUTION_MODE", "orchestrator"),
                        help="Evolution mode (default: orchestrator)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--no-daemon", action="store_true")
    parser.add_argument("--dev", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()

    os.environ["EVOLUTION_MODE"] = args.mode
    if args.no_daemon:
        os.environ["DAEMON_DISABLED"] = "1"

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
