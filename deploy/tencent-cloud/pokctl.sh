#!/usr/bin/env bash
# pokctl.sh — poker evolution runtime manager (no systemd)
#
# Usage:
#   ./pokctl.sh start    — start the evolution runtime in background
#   ./pokctl.sh stop     — stop the runtime (graceful)
#   ./pokctl.sh restart  — stop + start
#   ./pokctl.sh status   — show process + health
#   ./pokctl.sh logs     — tail logs (Ctrl-C to exit)
#
# This replaces the systemd service for environments where systemd/sudo
# management is impractical. The process runs under nohup in its own
# session, same as the documented manual launch.

set -euo pipefail

RUNTIME_DIR="/home/ubuntu/pok1/.evolution_pok"
PYTHON="/home/ubuntu/pok1/.venv/bin/python"
ENV_FILE="$RUNTIME_DIR/deploy/tencent-cloud/env.runtime"
ENV_LOCAL="$RUNTIME_DIR/deploy/tencent-cloud/env.runtime.local"
PID_FILE="$RUNTIME_DIR/web/core/results/.pokctl.pid"
LOG_FILE="$RUNTIME_DIR/web/core/results/pokctl.log"
HOST="127.0.0.1"
PORT="8000"

cd "$RUNTIME_DIR"

load_env() {
    set -a
    . "$ENV_FILE" 2>/dev/null || true
    if [ -f "$ENV_LOCAL" ]; then
        . "$ENV_LOCAL" 2>/dev/null || true
    fi
    set +a
}

is_running() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

cmd_start() {
    if is_running; then
        echo "Already running (PID $(cat "$PID_FILE"))"
        return 0
    fi
    load_env
    echo "Starting pok-evolution..."
    mkdir -p "$(dirname "$PID_FILE")"
    nohup setsid "$PYTHON" web/main.py \
        --host "$HOST" --port "$PORT" --no-build \
        >> "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    echo "Started (PID $pid). Logs: $LOG_FILE"
    echo "Waiting for startup..."
    sleep 5
    if kill -0 "$pid" 2>/dev/null; then
        echo "Process alive. Waiting for health (up to 120s)..."
        for i in $(seq 1 24); do
            if curl -sf --max-time 5 "http://$HOST:$PORT/api/control/health" >/dev/null 2>&1; then
                echo "Health endpoint responding (after ${i}x5s)."
                return 0
            fi
            sleep 5
        done
        echo "WARNING: health endpoint not responding after 120s. Check logs: tail -f $LOG_FILE"
    else
        echo "ERROR: process died immediately. Check logs: tail -50 $LOG_FILE"
        return 1
    fi
}

cmd_stop() {
    if ! is_running; then
        echo "Not running."
        rm -f "$PID_FILE"
        return 0
    fi
    local pid
    pid=$(cat "$PID_FILE")
    echo "Stopping (PID $pid)..."
    # Graceful: SIGTERM to the process group (includes daemon children)
    kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for i in $(seq 1 10); do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "Stopped."
            rm -f "$PID_FILE"
            return 0
        fi
        sleep 1
    done
    echo "Force killing..."
    kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "Force stopped."
}

cmd_status() {
    if is_running; then
        local pid
        pid=$(cat "$PID_FILE")
        echo "RUNNING (PID $pid)"
    else
        echo "STOPPED"
    fi
    echo "---"
    curl -sf --max-time 10 "http://$HOST:$PORT/api/control/health" 2>/dev/null | \
        "$PYTHON" -c "
import sys, json
try:
    d = json.load(sys.stdin)
    s = d.get('status', {})
    print('overall:', d.get('overall'))
    print('running:', s.get('running'))
    print('daemon_enabled:', s.get('daemon_enabled'))
    print('next_v:', s.get('next_v'))
    print('current_v:', s.get('current_v'))
except:
    print('(health endpoint not responding)')
" 2>/dev/null || echo "(health endpoint not responding)"
}

cmd_logs() {
    tail -f "$LOG_FILE"
}

case "${1:-}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_stop; cmd_start ;;
    status)  cmd_status ;;
    logs)    cmd_logs ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac
