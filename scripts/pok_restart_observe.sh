#!/usr/bin/env bash
# Safe restart + generation observer for the Pok evolution web app.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HOST="0.0.0.0"
PORT="8000"
NO_BUILD=1
DAEMON_WORKERS="12"
DAEMON_PAIRS="5"
CLEAR_SESSION="stale"
CLEAR_CHECKPOINT="never"
OBSERVE_GENERATIONS="3"
OBSERVE_TIMEOUT="21600"
DRY_RUN=0

usage() {
    cat <<EOF
Usage: $0 [options]

Options:
  --host HOST                     default: 0.0.0.0
  --port PORT                     default: 8000
  --build                         allow frontend build on startup
  --no-build                      skip frontend build on startup (default)
  --daemon-workers N              written to web/core/results/app_config.json (default: 12, clamped by app)
  --daemon-pairs N                written to web/core/results/app_config.json (default: 5)
  --clear-session stale|always|never
                                  stale = clear only when no pipeline checkpoint exists (default)
  --clear-checkpoint never|backup-and-clear
                                  default keeps pipeline_state.json
  --observe-generations N         terminal generation events to observe after restart (default: 3)
  --observe-timeout SEC           max observation seconds (default: 21600)
  --dry-run                       print actions only
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --build) NO_BUILD=0; shift ;;
        --no-build) NO_BUILD=1; shift ;;
        --daemon-workers) DAEMON_WORKERS="$2"; shift 2 ;;
        --daemon-pairs) DAEMON_PAIRS="$2"; shift 2 ;;
        --clear-session) CLEAR_SESSION="$2"; shift 2 ;;
        --clear-checkpoint) CLEAR_CHECKPOINT="$2"; shift 2 ;;
        --observe-generations) OBSERVE_GENERATIONS="$2"; shift 2 ;;
        --observe-timeout) OBSERVE_TIMEOUT="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

case "$CLEAR_SESSION" in stale|always|never) ;; *) echo "bad --clear-session" >&2; exit 2 ;; esac
case "$CLEAR_CHECKPOINT" in never|backup-and-clear) ;; *) echo "bad --clear-checkpoint" >&2; exit 2 ;; esac

LOG_DIR="web/logs"
RESULTS_DIR="web/core/results"
mkdir -p "$LOG_DIR" "$RESULTS_DIR"
TS="$(date +%Y%m%d_%H%M%S)"
RESTART_ID="restart-$TS-$$"
RUN_LOG="$LOG_DIR/restart_${TS}.log"
LOCK_FILE="$LOG_DIR/restart.lock"

log() {
    printf '[%s] [%s] %s\n' "$(date -Iseconds)" "$RESTART_ID" "$*" | tee -a "$RUN_LOG"
}

run() {
    log "+ $*"
    if [ "$DRY_RUN" = "0" ]; then
        "$@" 2>&1 | tee -a "$RUN_LOG"
    fi
}

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "Another restart is already running: $LOCK_FILE" >&2
    exit 1
fi

log "restart snapshot"
if [ -f "$LOG_DIR/.server.pid" ]; then
    log "server_pid=$(cat "$LOG_DIR/.server.pid")"
else
    log "server_pid=<missing>"
fi
if [ -f "$RESULTS_DIR/.daemon_pid" ]; then
    log "daemon_pid=$(cat "$RESULTS_DIR/.daemon_pid")"
else
    log "daemon_pid=<missing>"
fi
if [ -f "$RESULTS_DIR/pipeline_state.json" ]; then
    log "checkpoint=$(tr '\n' ' ' < "$RESULTS_DIR/pipeline_state.json" | cut -c1-500)"
else
    log "checkpoint=<missing>"
fi

if [ "$CLEAR_CHECKPOINT" = "backup-and-clear" ] && [ -f "$RESULTS_DIR/pipeline_state.json" ]; then
    run cp "$RESULTS_DIR/pipeline_state.json" "$RESULTS_DIR/pipeline_state.${TS}.bak.json"
    run rm -f "$RESULTS_DIR/pipeline_state.json"
fi

SESSION_FILE="$RESULTS_DIR/orchestrator_session.json"
if [ "$CLEAR_SESSION" = "always" ]; then
    run rm -f "$SESSION_FILE"
elif [ "$CLEAR_SESSION" = "stale" ] && [ ! -f "$RESULTS_DIR/pipeline_state.json" ]; then
    run rm -f "$SESSION_FILE"
else
    log "session policy kept existing session file if present"
fi

log "writing daemon config workers=$DAEMON_WORKERS pairs=$DAEMON_PAIRS"
if [ "$DRY_RUN" = "0" ]; then
    python - "$RESULTS_DIR/app_config.json" "$DAEMON_WORKERS" "$DAEMON_PAIRS" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
workers = int(sys.argv[2])
pairs = int(sys.argv[3])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({
    "daemon_enabled": True,
    "daemon_workers": workers,
    "daemon_pairs": pairs,
}, indent=2), encoding="utf-8")
PY
fi

START_ARGS=(--host "$HOST" --port "$PORT")
if [ "$NO_BUILD" = "1" ]; then
    START_ARGS+=(--no-build)
fi
run ./pokctl.sh restart "${START_ARGS[@]}"

if [ "$DRY_RUN" = "0" ]; then
    log "waiting for HTTP health"
    python - "$HOST" "$PORT" "$RUN_LOG" <<'PY'
import json
import sys
import time
import urllib.request

host, port, log_file = sys.argv[1], sys.argv[2], sys.argv[3]
url_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
url = f"http://{url_host}:{port}/api/control/status"
deadline = time.time() + 60
last = ""
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(f"[health] {url} {data}\n")
        print(f"health ok: {url}")
        raise SystemExit(0)
    except Exception as exc:
        last = str(exc)
        time.sleep(2)
print(f"health check failed: {last}", file=sys.stderr)
raise SystemExit(1)
PY
fi

if [ "$OBSERVE_GENERATIONS" -le 0 ]; then
    log "observe skipped (--observe-generations=$OBSERVE_GENERATIONS)"
    exit 0
fi

log "observing terminal generation events count=$OBSERVE_GENERATIONS timeout=${OBSERVE_TIMEOUT}s"
if [ "$DRY_RUN" = "0" ]; then
    python - "$RESULTS_DIR/events.jsonl" "$OBSERVE_GENERATIONS" "$OBSERVE_TIMEOUT" "$RUN_LOG" <<'PY'
import json
import pathlib
import sys
import time

events_file = pathlib.Path(sys.argv[1])
target = int(sys.argv[2])
timeout = int(sys.argv[3])
log_file = pathlib.Path(sys.argv[4])
terminal = {
    "pipeline.commit_done",
    "pipeline.archived",
    "pipeline.quality_failed",
    "pipeline.cycle_timeout",
    "pipeline.cycle_timeout_abandon",
    "orchestrator.crashed",
    "daemon.crashed",
}
seen = 0
pos = events_file.stat().st_size if events_file.exists() else 0
deadline = time.time() + timeout
while time.time() < deadline and seen < target:
    if not events_file.exists():
        time.sleep(2)
        continue
    with events_file.open("r", encoding="utf-8") as fh:
        fh.seek(pos)
        while True:
            line = fh.readline()
            if not line:
                break
            pos = fh.tell()
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("type") in terminal:
                seen += 1
                msg = f"[observe] {seen}/{target} {event.get('type')} {event.get('message')} data={event.get('data', {})}"
                print(msg)
                with log_file.open("a", encoding="utf-8") as out:
                    out.write(msg + "\n")
    time.sleep(3)
if seen < target:
    print(f"observe timeout: saw {seen}/{target} terminal events", file=sys.stderr)
    raise SystemExit(1)
PY
fi
