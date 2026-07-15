"""Web entry point for the national_tcp_policy_v1 evolution control plane.

Usage:
    python web/main.py                      # Orchestrator mode on port 8000
    python web/main.py --port 3000          # Custom port
    python web/main.py --no-daemon          # Run orchestrator without background rating daemon
    python web/main.py --view-only          # Serve dashboard/API without starting evolution
    python web/main.py --no-build           # Skip frontend build
    python web/main.py --dev                # Enable auto-reload
"""

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("pok.main")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(WEB_DIR / "core"))


def apply_cli_runtime_overrides(app_state, *, no_daemon: bool) -> dict:
    """Apply only explicit process-local launcher overrides.

    An omitted ``--no-daemon`` means "use the persisted operator setting",
    not "force-enable the daemon".  This distinction keeps the launcher,
    control API, dashboard, and actual orchestrator arguments on one config.
    """

    if no_daemon:
        return app_state.override_runtime_config(daemon_enabled=False)
    return app_state.get_config()


def build_frontend() -> bool:
    """Build the frontend and copy dist to server/static. Returns True on success."""
    frontend_dir = WEB_DIR / "frontend"
    if not (frontend_dir / "package.json").exists():
        log.warning("[build] package.json not found, skipping frontend build.")
        return False

    if not (frontend_dir / "node_modules").is_dir():
        log.info("[build] node_modules missing; installing frontend dependencies with npm ci...")
        install = subprocess.run(
            ["npm", "ci"],
            cwd=str(frontend_dir),
            capture_output=True,
            text=True,
        )
        if install.returncode != 0:
            log.error("[build] Frontend dependency install failed: %s", install.stderr)
            return False

    log.info("[build] Building frontend...")
    result = subprocess.run(
        ["npm", "run", "build"],
        cwd=str(frontend_dir),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.error("[build] Frontend build failed: %s", result.stderr)
        return False

    log.info("[build] Frontend build complete.")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="national_tcp_policy_v1 evolution Web/API control plane"
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--no-daemon", action="store_true", help="Run orchestrator without the background rating daemon")
    parser.add_argument("--view-only", action="store_true", help="Serve dashboard/API without starting the orchestrator or daemon")
    parser.add_argument("--no-build", action="store_true", help="Skip frontend build on startup")
    parser.add_argument("--dev", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()

    # The autonomous checkout is publication-authoritative.  Directly using
    # the documented ``python web/main.py`` entrypoint there must retain the
    # same push-required semantics as the restart helper; otherwise commit_bot
    # can create a local-only tag while the dashboard claims publication.
    if PROJECT_ROOT.name == ".evolution_pok":
        os.environ.setdefault("POK_EVOLUTION_RUNTIME", "1")
        os.environ.setdefault("POK_REQUIRE_EVOLUTION_PUSH", "1")
        os.environ.setdefault("EVOLUTION_GIT_PUSH", "1")

    import uvicorn

    if args.view_only:
        os.environ["POK_WEB_VIEW_ONLY"] = "1"
        args.no_daemon = True

    if args.no_daemon:
        os.environ["DAEMON_DISABLED"] = "1"
    os.environ.setdefault("POK_WORKFLOW_PROFILE", "national_native")

    # Auto-build frontend before starting server
    if not args.no_build:
        if not build_frontend():
            sys.exit(1)

    # Initialize structured logging
    from logging_config import configure_logging
    configure_logging(dev_mode=args.dev)

    # Pre-populate app_state from CLI args so lifespan reads correct config
    sys.path.insert(0, str(WEB_DIR / "server"))
    from server.state import app_state
    apply_cli_runtime_overrides(app_state, no_daemon=args.no_daemon)

    # Note: atexit.register(stop_daemon) is handled inside start_daemon() itself
    # (daemon_management.py line ~90) — no need to register again here.
    # Duplicate registration causes stop_daemon to run 2x on exit (1s wasted in orphan checks).

    uvicorn.run(
        "server.app:app",
        host=args.host,
        port=args.port,
        reload=args.dev,
    )


if __name__ == "__main__":
    main()
