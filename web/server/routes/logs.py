"""Log endpoints — generation logs browsing, orchestrator logs, system events, and worker failures."""

import json
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from evolution_infra import locked_file

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


def _infer_category_from_type(event_type: str) -> str:
    """Infer a dot-prefixed category from a legacy event type with no category.

    "pipeline.master_planned" -> "pipeline.", "daemon.save" -> "daemon.".
    Returns the first dot-delimited segment + trailing "." (or the whole type
    if there is no dot).
    """
    if not event_type:
        return ""
    if "." in event_type:
        return event_type.split(".", 1)[0] + "."
    return event_type + "."


@router.get("/logs/system-events")
async def get_system_events(
    type: str = Query("", description="Filter by event type prefix (e.g. pipeline.)"),
    category: str = Query("", description="Filter by data.category or type-prefix category (e.g. pipeline.)"),
    severity: str = Query("", description="Filter by severity: info|warn|error|success"),
    source: str = Query("legacy", description="Event source: legacy|structured"),
    run_id: str = Query("", description="Filter by data.run_id, e.g. 231#0"),
    stage: str = Query("", description="Filter by data.stage"),
    since: float | None = Query(None, description="Only events after this Unix timestamp"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    from system_log import SYSTEM_EVENTS_FILE
    if source not in {"legacy", "structured"}:
        raise HTTPException(status_code=400, detail="source must be 'legacy' or 'structured'")
    events_file = RESULTS_DIR / "events.jsonl" if source == "structured" else SYSTEM_EVENTS_FILE
    if not events_file.exists():
        return {"events": [], "total": 0}
    events = []
    with locked_file(events_file, "r") as f:
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
            # Category dimension: backfill legacy rows (no data.category) from
            # the event type's first segment. In-memory only, not written to disk.
            data = entry.get("data") or {}
            cat = data.get("category") if isinstance(data, dict) else None
            if not cat:
                cat = _infer_category_from_type(entry.get("type", ""))
                data = dict(data)
                data["category"] = cat
                entry["data"] = data
            if category and not cat.startswith(category):
                continue
            if run_id and data.get("run_id") != run_id:
                continue
            if stage and data.get("stage") != stage:
                continue
            events.append(entry)
    events.reverse()
    total = len(events)
    return {"events": events[offset:offset + limit], "total": total}


def _infer_category_from_role(role: str) -> str:
    """Backfill a worker_failures category from a legacy row's role.

    Critic/Reviewer rows belong to gates ("gate"), everything else is a worker
    ("worker").
    """
    if not role:
        return "worker"
    r = role.lower()
    if "critic" in r or "reviewer" in r:
        return "gate"
    return "worker"


@router.get("/logs/worker-failures")
async def get_worker_failures(
    gen: int = Query(None, description="Filter by generation number"),
    role: str = Query("", description="Filter by role name"),
    category: str = Query("", description="Filter by category: worker|gate"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    from evolution_infra import WORKER_FAILURES_FILE
    failures_file = WORKER_FAILURES_FILE
    if not failures_file.exists():
        return {"failures": [], "total": 0}
    failures = []
    with locked_file(failures_file, "r") as f:
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
            # Category dimension: backfill legacy rows (no category) from role.
            # In-memory only, not written to disk.
            cat = entry.get("category")
            if not cat:
                cat = _infer_category_from_role(entry.get("role", ""))
                entry["category"] = cat
            if category and cat != category:
                continue
            failures.append(entry)
    failures.reverse()
    total = len(failures)
    return {"failures": failures[offset:offset + limit], "total": total}
