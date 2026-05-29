#!/bin/bash
# Start the Evolution Dashboard (with optional Evolution loop)
#
# Usage:
#   ./start.sh                  # dev: dashboard + evolution
#   ./start.sh --build          # prod: dashboard + evolution
#   ./start.sh --no-evolve      # dev: dashboard only
#   ./start.sh --build --no-evolve  # prod: dashboard only

set -e

# Resolve absolute paths
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

BUILD=false
NO_EVOLVE=false
for arg in "$@"; do
    case "$arg" in
        --build)      BUILD=true ;;
        --no-evolve)  NO_EVOLVE=true ;;
    esac
done

PIDS=()

cleanup() {
    echo ""
    echo "Shutting down..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null
    done
    sleep 1
    for pid in "${PIDS[@]}"; do
        kill -9 "$pid" 2>/dev/null
    done
    exit 0
}
trap cleanup SIGINT SIGTERM

# Export evolution control via environment variable
if $NO_EVOLVE; then
    export EVOLUTION_DISABLED=1
fi

if $BUILD; then
    # ── Production mode ──
    echo "Building frontend for production..."
    cd "$SCRIPT_DIR/frontend" && npm run build
    mkdir -p "$SCRIPT_DIR/backend/static"
    cp -r "$SCRIPT_DIR/frontend/dist/"* "$SCRIPT_DIR/backend/static/"

    echo ""
    if $NO_EVOLVE; then
        echo "Starting production dashboard on http://localhost:8000 (no evolution)"
    else
        echo "Starting production dashboard + evolution on http://localhost:8000"
    fi
    cd "$PROJECT_ROOT"
    exec python -m uvicorn dashboard.backend.app:app --host 0.0.0.0 --port 8000
else
    # ── Development mode ──
    echo "Starting development servers..."
    echo "  Backend + Evolution: http://localhost:8000"
    echo "  Frontend dev:        http://localhost:5173"
    if ! $NO_EVOLVE; then
        echo "  Evolution:           enabled (integrated)"
    else
        echo "  Evolution:           disabled"
    fi
    echo ""

    # Backend with integrated evolution (env var controls whether evolution runs)
    (cd "$PROJECT_ROOT" && python -m uvicorn dashboard.backend.app:app --port 8000 --reload) &
    PIDS+=($!)

    # Frontend dev server
    (cd "$SCRIPT_DIR/frontend" && npx vite --host) &
    PIDS+=($!)

    wait
fi
