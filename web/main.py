"""Unified web entry point for the Poker Evolution Framework.

Usage:
    python web/main.py                      # Orchestrator mode on port 8000
    python web/main.py --port 3000          # Custom port
    python web/main.py --no-daemon          # No background daemon
    python web/main.py --no-build           # Skip frontend build
    python web/main.py --dev                # Enable auto-reload
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
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--no-daemon", action="store_true")
    parser.add_argument("--no-build", action="store_true", help="Skip frontend build on startup")
    parser.add_argument("--dev", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()

    import uvicorn

    if args.no_daemon:
        os.environ["DAEMON_DISABLED"] = "1"

    # Auto-build frontend before starting server
    if not args.no_build:
        if not build_frontend():
            sys.exit(1)

    # Pre-populate app_state from CLI args so lifespan reads correct config
    sys.path.insert(0, str(WEB_DIR / "server"))
    from server.state import app_state
    app_state.update_config(daemon_enabled=not args.no_daemon)

    uvicorn.run(
        "server.app:app",
        host=args.host,
        port=args.port,
        reload=args.dev,
    )


if __name__ == "__main__":
    main()
