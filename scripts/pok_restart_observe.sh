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
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log "git snapshot before restart"
    if [ "$DRY_RUN" = "0" ]; then
        git status --short --branch | head -50 | sed 's/^/[git] /' | tee -a "$RUN_LOG"
    else
        log "+ git status --short --branch | head -50"
    fi
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

# Do not let the long-lived web server inherit the restart lock fd. Bash file
# descriptors are inherited by child processes by default; if web/main.py keeps
# fd 9 open, every later restart sees restart.lock as permanently held.
log "releasing restart lock before spawning web service"
flock -u 9
exec 9>&-
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
url = f"http://{url_host}:{port}/api/control/health"
deadline = time.time() + 60
last = ""
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(f"[health] {url} {data}\n")
        print(f"health ok: {url} overall={data.get('overall')}")
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
    python - "$RESULTS_DIR/events.jsonl" "$OBSERVE_GENERATIONS" "$OBSERVE_TIMEOUT" "$RUN_LOG" \
        "$LOG_DIR/.server.pid" "$RESULTS_DIR/.daemon_pid" "$RESULTS_DIR/pipeline_state.json" "$HOST" "$PORT" <<'PY'
import json
import pathlib
import re
import sys
import time
import urllib.request

events_file = pathlib.Path(sys.argv[1])
target = int(sys.argv[2])
timeout = int(sys.argv[3])
log_file = pathlib.Path(sys.argv[4])
server_pid_file = pathlib.Path(sys.argv[5])
daemon_pid_file = pathlib.Path(sys.argv[6])
pipeline_state_file = pathlib.Path(sys.argv[7])
host = sys.argv[8]
port = sys.argv[9]
url_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
health_url = f"http://{url_host}:{port}/api/control/health"
generation_terminal = {
    "pipeline.commit_done",
    "pipeline.archivist_done",
    "pipeline.abandoned",
    "pipeline.master_exhausted",
    "pipeline.master_audit_exhausted_abandon",
    "pipeline.cycle_timeout",
    "pipeline.cycle_timeout_abandon",
    "pipeline.precommit_hard_limit",
}
alert_events = {
    "daemon.crashed",
    "daemon.exited_cleanly",
    "orchestrator.crashed",
    "pipeline.quality_failed",
    "pipeline.guard_block",
    "pipeline.subagent_guard_block",
    "pipeline.redundant_tool_call",
    "pipeline.sdk_stream_error",
    "pipeline.llm_role_cancelled",
    "pipeline.llm_role_stream_cancelled",
    "pipeline.precommit_eval",
    "pipeline.precommit_infra_timeout",
}
fatal_events = {
    "pipeline.llm_role_cancelled",
    "pipeline.llm_role_stream_cancelled",
}
stage_stale_limits = {
    "prepared": 900,
    "direction_audited": 1500,
    "master_planned": 1500,
    "workers_done": 1500,
    "quality_failed": 1200,
    "quality_passed": 1200,
    "reviewed": 1200,
    "critic_checked": 2700,
    "verified": 900,
}
default_stage_stale_limit = 3600
seen = 0
seen_generations = set()
pos = events_file.stat().st_size if events_file.exists() else 0
deadline = time.time() + timeout
last_service_check = 0.0
last_heartbeat = 0.0
http_fail_count = 0
first_http_fail = None
service_check_interval = 15
heartbeat_interval = 60
http_fail_limit = 3
http_fail_grace_sec = 300
terminal_events = []
alert_records = []
stage_transitions = []
last_stage_key = None
checkpoint_missing_since = None

def write_line(msg):
    print(msg)
    with log_file.open("a", encoding="utf-8") as out:
        out.write(msg + "\n")

def compact_summary(reason):
    terminal_counts = {}
    for item in terminal_events:
        terminal_counts[item.get("type", "unknown")] = terminal_counts.get(item.get("type", "unknown"), 0) + 1
    return {
        "observed": seen,
        "target": target,
        "reason": reason,
        "terminal_counts": terminal_counts,
        "alert_count": len(alert_records),
        "stage_transition_count": len(stage_transitions),
        "last_stage": stage_transitions[-1]["pipeline"] if stage_transitions else {},
    }

def write_compact_summary(reason):
    write_line("[summary-compact] " + json.dumps(compact_summary(reason), ensure_ascii=False, default=str))

def read_pid(path):
    if not path.exists():
        return None, "missing"
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return None, "empty"
    try:
        data = json.loads(text)
        value = data.get("pid") if isinstance(data, dict) else data
    except Exception:
        match = re.search(r"\d+", text)
        value = match.group(0) if match else None
    try:
        pid = int(value)
    except Exception:
        return None, f"bad:{text[:80]}"
    if pid <= 1:
        return None, f"unsafe:{pid}"
    return pid, "ok"

def pid_alive(pid):
    return bool(pid and pathlib.Path(f"/proc/{pid}").exists())

def proc_summary(pid):
    if not pid:
        return {}
    proc = pathlib.Path(f"/proc/{pid}")
    summary = {"pid": pid, "alive": proc.exists()}
    if not proc.exists():
        return summary
    try:
        stat = (proc / "stat").read_text(encoding="utf-8", errors="replace").split()
        summary["ppid"] = int(stat[3])
        summary["pgid"] = int(stat[4])
        summary["sid"] = int(stat[5])
    except Exception:
        pass
    try:
        cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        summary["cmd"] = cmdline[:240]
    except Exception:
        pass
    return summary

def http_health():
    try:
        with urllib.request.urlopen(health_url, timeout=2) as resp:
            body = resp.read().decode("utf-8", "replace")
        data = json.loads(body)
        return {"ok": True, "status": data}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

def pipeline_snapshot():
    if not pipeline_state_file.exists():
        return {"checkpoint": "missing"}
    try:
        state = json.loads(pipeline_state_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"checkpoint": f"bad:{type(exc).__name__}"}
    keys = ("run_id", "stage", "next_v", "source_v", "generation_attempt", "audit_attempt", "precommit_attempt")
    return {key: state.get(key) for key in keys if key in state}

def check_service(force_heartbeat=False):
    global last_service_check, last_heartbeat, http_fail_count, first_http_fail, last_stage_key, checkpoint_missing_since
    now = time.time()
    if now - last_service_check < service_check_interval and not force_heartbeat:
        return
    last_service_check = now
    server_pid, server_note = read_pid(server_pid_file)
    daemon_pid, daemon_note = read_pid(daemon_pid_file)
    server_alive = pid_alive(server_pid)
    daemon_alive = pid_alive(daemon_pid)
    status = http_health()
    snapshot = {
        "server_pid": server_pid,
        "server_pid_note": server_note,
        "server_alive": server_alive,
        "server_proc": proc_summary(server_pid),
        "daemon_pid": daemon_pid,
        "daemon_pid_note": daemon_note,
        "daemon_alive": daemon_alive,
        "daemon_proc": proc_summary(daemon_pid),
        "http": status,
        "pipeline": pipeline_snapshot(),
    }
    if not server_alive:
        write_line(f"[service-dead] observed web service is unavailable: {snapshot}")
        write_compact_summary("web_pid_dead")
        raise SystemExit(1)
    if not status.get("ok"):
        http_fail_count += 1
        if first_http_fail is None:
            first_http_fail = now
        fail_age = now - first_http_fail
        if http_fail_count >= http_fail_limit and fail_age >= http_fail_grace_sec:
            write_line(f"[service-dead] observed web service HTTP status failed {http_fail_count} times over {fail_age:.0f}s: {snapshot}")
            write_compact_summary("http_status_dead")
            raise SystemExit(1)
        write_line(f"[service-http-warning] observed web service HTTP status failed {http_fail_count}/{http_fail_limit}, age={fail_age:.0f}/{http_fail_grace_sec}s: {snapshot}")
        return
    http_fail_count = 0
    first_http_fail = None
    health = status.get("status") or {}
    health_issues = set(health.get("issues") or [])
    if health.get("running") is False or health.get("overall") == "stopped":
        write_line(f"[service-stopped] evolution is not running: {snapshot}")
        write_compact_summary("evolution_not_running")
        raise SystemExit(1)
    fatal_health = {
        "orchestrator_task_not_active",
        "daemon_dead",
        "daemon_heartbeat_stale",
        "pipeline_checkpoint_unreadable",
        "active_generation_without_checkpoint",
    }
    matched_health = sorted(health_issues & fatal_health)
    if matched_health:
        write_line(f"[service-degraded] fatal health issue(s) {matched_health}: {snapshot}")
        write_compact_summary("fatal_health_issue")
        raise SystemExit(1)
    if force_heartbeat or now - last_heartbeat >= heartbeat_interval:
        last_heartbeat = now
        write_line(f"[service-heartbeat] {snapshot}")
    pipeline = (health.get("pipeline") or snapshot.get("pipeline") or {})
    if not pipeline.get("exists") and health.get("running"):
        checkpoint_missing_since = checkpoint_missing_since or now
        missing_age = now - checkpoint_missing_since
        if missing_age > 900:
            write_line(f"[checkpoint-missing] pipeline checkpoint missing for {missing_age:.0f}s while running: {snapshot}")
            write_compact_summary("checkpoint_missing")
            raise SystemExit(1)
    else:
        checkpoint_missing_since = None
    stage = pipeline.get("stage")
    stage_age = pipeline.get("last_stage_age_sec")
    if stage and stage_age is not None:
        limit = stage_stale_limits.get(stage, default_stage_stale_limit)
        try:
            if float(stage_age) > limit:
                write_line(f"[stage-stale] stage={stage} age={stage_age}s limit={limit}s: {snapshot}")
                write_compact_summary("stage_stale")
                raise SystemExit(1)
        except SystemExit:
            raise
        except Exception:
            pass
    stage_key = (
        pipeline.get("run_id"),
        pipeline.get("next_v"),
        pipeline.get("stage"),
        pipeline.get("precommit_attempt"),
    )
    if stage_key != last_stage_key:
        last_stage_key = stage_key
        stage_transitions.append({"ts": round(now, 1), "pipeline": pipeline})
        write_line(f"[stage] {pipeline}")

def generation_key(event):
    data = event.get("data") or {}
    for key in ("abandoned_v", "version", "next_v", "target_v"):
        value = data.get(key)
        if value is not None:
            return f"v{value}"
    run_id = data.get("run_id")
    if run_id:
        return f"run:{run_id}"
    return f"{event.get('type')}:{int(event.get('ts') or 0)}"

def should_alert(event):
    etype = event.get("type")
    if etype == "pipeline.precommit_eval":
        return not bool((event.get("data") or {}).get("passed", True))
    return etype in alert_events

while time.time() < deadline and seen < target:
    check_service()
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
            if should_alert(event):
                msg = f"[alert] {event.get('type')} {event.get('message')} data={event.get('data', {})}"
                alert_records.append({
                    "type": event.get("type"),
                    "message": event.get("message"),
                    "data": event.get("data", {}),
                })
                write_line(msg)
            if event.get("type") in fatal_events:
                write_line(f"[fatal-event] {event.get('type')} {event.get('message')} data={event.get('data', {})}")
                write_compact_summary("fatal_event")
                raise SystemExit(1)
            if event.get("type") in generation_terminal:
                key = generation_key(event)
                if key in seen_generations:
                    msg = f"[observe] duplicate terminal for {key}: {event.get('type')} {event.get('message')}"
                    write_line(msg)
                    continue
                seen_generations.add(key)
                seen += 1
                terminal_events.append({
                    "key": key,
                    "type": event.get("type"),
                    "message": event.get("message"),
                    "data": event.get("data", {}),
                })
                msg = f"[observe] {seen}/{target} {key} {event.get('type')} {event.get('message')} data={event.get('data', {})}"
                write_line(msg)
    time.sleep(3)
if seen < target:
    check_service(force_heartbeat=True)
    print(f"observe timeout: saw {seen}/{target} terminal events", file=sys.stderr)
    write_compact_summary("observe_timeout")
    raise SystemExit(1)
summary = {
    "observed": seen,
    "target": target,
    "terminal_events": terminal_events,
    "alerts": alert_records[-20:],
    "stage_transitions": stage_transitions[-20:],
}
write_compact_summary("target_reached")
write_line("[summary] " + json.dumps(summary, ensure_ascii=False, default=str))
PY
fi
