"""Log endpoints — generation logs browsing, orchestrator logs, system events, and worker failures."""

import json
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
ORCHESTRATOR_LOGS_DIR = PROJECT_ROOT / "web" / "logs"

router = APIRouter(prefix="/api", tags=["logs"])


@router.get("/logs/generations")
async def list_generations():
    from server.routes._helpers import list_generation_dirs
    return list_generation_dirs(RESULTS_DIR)


@router.get("/logs/generations/{version}/{filename}")
async def get_log(version: str, filename: str, tail: int = Query(0, ge=0)):
    # Resolve to prevent path traversal (e.g. version="../../etc")
    resolved = (RESULTS_DIR / version / "logs" / filename).resolve()
    if not resolved.is_relative_to(RESULTS_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Invalid path")
    path = resolved
    if not path.is_file():
        return {"version": version, "filename": filename, "content": ""}
    with open(path, "r") as f:
        if tail > 0:
            lines = f.readlines()
            content = "".join(lines[-tail:])
        else:
            content = f.read()
    return {"version": version, "filename": filename, "content": content}


@router.get("/logs/orchestrator")
async def list_orchestrator_logs():
    """List orchestrator log files (most recent first)."""
    if not ORCHESTRATOR_LOGS_DIR.exists():
        return []
    files = sorted(
        (f for f in ORCHESTRATOR_LOGS_DIR.iterdir()
         if f.is_file() and f.name.startswith("orchestrator_") and f.name.endswith(".txt")),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    return [
        {
            "filename": f.name,
            "size_bytes": f.stat().st_size,
            "mtime": f.stat().st_mtime,
        }
        for f in files[:20]
    ]


@router.get("/logs/orchestrator/{filename}", response_class=PlainTextResponse)
async def get_orchestrator_log(filename: str, tail: int = Query(0, ge=0)):
    """Get orchestrator log content. filename must be orchestrator_*.txt."""
    if not filename.startswith("orchestrator_") or not filename.endswith(".txt") or "/" in filename:
        return PlainTextResponse("Invalid filename", status_code=400)
    path = ORCHESTRATOR_LOGS_DIR / filename
    if not path.is_file():
        return PlainTextResponse("File not found", status_code=404)
    content = path.read_text(errors="replace")
    if tail > 0:
        lines = content.splitlines()
        content = "\n".join(lines[-tail:])
    return PlainTextResponse(content)


@router.get("/logs/system-events")
async def get_system_events(
    type: str = Query("", description="Filter by event type prefix (e.g. pipeline.)"),
    severity: str = Query("", description="Filter by severity: info|warn|error|success"),
    since: float | None = Query(None, description="Only events after this Unix timestamp"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    from system_log import SYSTEM_EVENTS_FILE
    events_file = SYSTEM_EVENTS_FILE
    if not events_file.exists():
        return {"events": [], "total": 0}
    import fcntl
    events = []
    with open(events_file, "r") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if type and not entry.get("type", "").startswith(type):
                    continue
                if severity and entry.get("severity") != severity:
                    continue
                if since is not None and entry.get("ts", 0) < since:
                    continue
                events.append(entry)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    events.reverse()
    total = len(events)
    return {"events": events[offset:offset + limit], "total": total}


@router.get("/logs/worker-failures")
async def get_worker_failures(
    gen: int = Query(None, description="Filter by generation number"),
    role: str = Query("", description="Filter by role name"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    from evolution_infra import WORKER_FAILURES_FILE
    failures_file = WORKER_FAILURES_FILE
    if not failures_file.exists():
        return {"failures": [], "total": 0}
    import fcntl
    failures = []
    with open(failures_file, "r") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if gen is not None and entry.get("gen") != gen:
                    continue
                if role and role.lower() not in entry.get("role", "").lower():
                    continue
                failures.append(entry)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    failures.reverse()
    total = len(failures)
    return {"failures": failures[offset:offset + limit], "total": total}
