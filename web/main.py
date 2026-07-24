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
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("pok.main")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = WEB_DIR / "frontend"
STATIC_DIR = WEB_DIR / "server" / "static"
STATIC_BUILD_RECEIPT = STATIC_DIR / ".pok-static-build-receipt.json"
STATIC_BUILD_RECEIPT_VERIFIER = (
    FRONTEND_DIR / "scripts" / "static-build-receipt.mjs"
)

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


def verify_frontend_static_receipt() -> bool:
    """Fail closed when ``--no-build`` would serve an unbound SPA bundle.

    ``pokctl.sh`` already performs this preflight before it stops an owned
    service.  The documented direct launcher must enforce the same source
    binding too: otherwise ``python web/main.py --no-build`` can start an app
    with stale dashboard status-authority code.  The Node verifier owns the
    receipt schema and source fingerprint calculation, so this launcher only
    owns the startup gate rather than duplicating shell or JavaScript logic.
    """

    index_html = STATIC_DIR / "index.html"
    assets_dir = STATIC_DIR / "assets"
    if not index_html.is_file() or not assets_dir.is_dir():
        log.error(
            "[build] --no-build requires %s and %s",
            index_html,
            assets_dir,
        )
        return False

    node = shutil.which("node")
    if node is None:
        log.error(
            "[build] --no-build requires Node.js to verify the source-bound "
            "frontend static receipt."
        )
        return False
    if not STATIC_BUILD_RECEIPT_VERIFIER.is_file():
        log.error(
            "[build] --no-build static receipt verifier is missing: %s",
            STATIC_BUILD_RECEIPT_VERIFIER,
        )
        return False

    try:
        verification = subprocess.run(
            [
                node,
                str(STATIC_BUILD_RECEIPT_VERIFIER),
                "--verify",
                str(STATIC_BUILD_RECEIPT),
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        log.error("[build] --no-build receipt verifier could not start: %s", exc)
        return False
    if verification.returncode != 0:
        detail = (verification.stderr or verification.stdout).strip()
        log.error(
            "[build] --no-build static bundle receipt is missing, malformed, "
            "or does not match current frontend sources%s",
            f": {detail}" if detail else "",
        )
        return False
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

    # Validate an explicitly reused bundle before importing the app, starting
    # the orchestrator, or handing control to Uvicorn.  A normal build owns
    # writing/copying its receipt atomically as part of ``npm run build``.
    if args.no_build:
        if not verify_frontend_static_receipt():
            sys.exit(1)
    elif not build_frontend():
        sys.exit(1)

    # The autonomous checkout is publication-authoritative.  Directly using
    # the documented ``python web/main.py`` entrypoint there must retain the
    # same push-required semantics as the restart helper; otherwise commit_bot
    # can create a local-only tag while the dashboard claims publication.
    if PROJECT_ROOT.name == ".evolution_pok":
        os.environ.setdefault("POK_EVOLUTION_RUNTIME", "1")
        os.environ.setdefault("POK_REQUIRE_EVOLUTION_PUSH", "1")
        os.environ.setdefault("EVOLUTION_GIT_PUSH", "1")
        # When a deployment opts into the isolated cloud-namespace evolution
        # line (POK_CLOUD_RUNTIME=1), seed the configurable namespace/branch
        # variables before any web.core module imports bot_namespace, so the
        # whole runtime publishes into the tencent-cloud-runtime branch with a
        # national_cloud_v tag namespace that never collides with origin/main.
        # These are setdefault only: an explicit EnvironmentFile value wins.
        if os.environ.get("POK_CLOUD_RUNTIME", "").lower() in {"1", "true", "yes", "on"}:
            os.environ.setdefault("POK_EVOLUTION_BRANCH", "tencent-cloud-runtime")
            os.environ.setdefault("POK_BOT_PREFIX", "national_cloud_v")
            os.environ.setdefault("POK_TAG_PREFIX", "national-cloud-bot-v")
            os.environ.setdefault("POK_HIGH_WATER_TAG_PREFIX", "national-cloud-high-water-v")

    if args.view_only:
        os.environ["POK_WEB_VIEW_ONLY"] = "1"
        args.no_daemon = True

    if args.no_daemon:
        os.environ["DAEMON_DISABLED"] = "1"
    os.environ.setdefault("POK_WORKFLOW_PROFILE", "national_native")

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

    import uvicorn

    uvicorn.run(
        "server.app:app",
        host=args.host,
        port=args.port,
        reload=args.dev,
    )


if __name__ == "__main__":
    main()
