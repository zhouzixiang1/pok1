"""Log endpoints — generation logs browsing, orchestrator logs, system events, and worker failures."""

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from evolution_infra import locked_file

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
ORCHESTRATOR_LOGS_DIR = PROJECT_ROOT / "web" / "logs"

router = APIRouter(prefix="/api", tags=["logs"])


def _current_log_epoch_identity() -> dict | None:
    try:
        from log_epoch import load_current_log_epoch_identity

        return load_current_log_epoch_identity(RESULTS_DIR, ORCHESTRATOR_LOGS_DIR)
    except Exception:
        return None


def _current_event_epoch_identity() -> dict | None:
    try:
        from system_strict_bootstrap import load_policy_epoch_reset_receipt

        receipt, errors = load_policy_epoch_reset_receipt(RESULTS_DIR)
    except Exception:
        return None
    if errors or not isinstance(receipt, dict):
        return None
    return {
        "evaluation_epoch": receipt.get("epoch"),
        "epoch_reset_receipt_digest": receipt.get("receipt_digest"),
    }


@router.get("/logs/generations")
async def list_generations():
    from server.routes._helpers import (
        list_generation_dirs,
        strict_observable_generation_versions,
    )

    allowed = strict_observable_generation_versions(
        RESULTS_DIR,
        RESULTS_DIR / "pipeline_state.json",
    )
    return list_generation_dirs(RESULTS_DIR, allowed_versions=allowed)


@router.get("/logs/generations/{version}/{filename}")
async def get_log(version: str, filename: str, tail: int = Query(0, ge=0)):
    from server.routes._helpers import strict_observable_generation_versions

    if re.fullmatch(r"v[1-9][0-9]*", version) is None:
        raise HTTPException(status_code=400, detail="Invalid generation")
    allowed = strict_observable_generation_versions(
        RESULTS_DIR,
        RESULTS_DIR / "pipeline_state.json",
    )
    if int(version[1:]) not in allowed:
        raise HTTPException(status_code=404, detail="Generation not observable")
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
    if _current_log_epoch_identity() is None or not ORCHESTRATOR_LOGS_DIR.exists():
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
    if _current_log_epoch_identity() is None:
        return PlainTextResponse("Log epoch unavailable", status_code=404)
    path = ORCHESTRATOR_LOGS_DIR / filename
    if path.is_symlink() or not path.is_file():
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


def _normalise_structured_event(entry: dict) -> dict:
    """Return an in-memory compatibility-normalised structured event row."""
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    if (
        entry.get("type") == "pipeline.llm_role_shutdown_cancelled"
        and data.get("shutdown_requested") is False
    ):
        data = dict(data)
        data.setdefault("original_type", entry.get("type"))
        data.setdefault("original_category", data.get("category"))
        data["legacy_misclassified"] = True
        data["corrected_type"] = "pipeline.llm_role_process_terminated"
        data["category"] = "pipeline.llm_role_process_terminated"
        entry = dict(entry)
        entry["type"] = "pipeline.llm_role_process_terminated"
        entry["severity"] = "error"
        entry["data"] = data
    return entry


@router.get("/logs/system-events")
async def get_system_events(
    type: str = Query("", description="Filter by event type prefix (e.g. pipeline.)"),
    category: str = Query("", description="Filter by data.category or type-prefix category (e.g. pipeline.)"),
    severity: str = Query("", description="Filter by severity: info|warn|error|success"),
    source: str = Query("structured", description="Canonical event source; only 'structured' is valid"),
    run_id: str = Query("", description="Filter by data.run_id, e.g. 231#0"),
    stage: str = Query("", description="Filter by data.stage"),
    since: float | None = Query(None, description="Only events after this Unix timestamp"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    if source != "structured":
        raise HTTPException(status_code=400, detail="source must be 'structured'")
    identity = _current_event_epoch_identity()
    if identity is None:
        return {
            "events": [],
            "total": 0,
            "authority_status": "policy_epoch_not_initialized",
        }
    events_file = RESULTS_DIR / "events.jsonl"
    if not events_file.exists():
        return {
            "events": [],
            "total": 0,
            "authority_status": "current_epoch_empty",
            **identity,
        }
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
            event_data = entry.get("data")
            if not isinstance(event_data, dict) or (
                event_data.get("evaluation_epoch")
                != identity["evaluation_epoch"]
                or event_data.get("epoch_reset_receipt_digest")
                != identity["epoch_reset_receipt_digest"]
            ):
                continue
            entry = _normalise_structured_event(entry)
            if type and not entry.get("type", "").startswith(type):
                continue
            if severity and entry.get("severity") != severity:
                continue
            if since is not None and entry.get("ts", 0) < since:
                continue
            # Old canonical rows may predate data.category. Backfill only in the
            # response; never create a second compatibility ledger.
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
    return {
        "events": events[offset:offset + limit],
        "total": total,
        "authority_status": "current_epoch",
        **identity,
    }


@router.get("/logs/worker-failures")
async def get_worker_failures(
    gen: int = Query(None, description="Filter by generation number"),
    role: str = Query("", description="Filter by role name"),
    category: str = Query("", description="Filter by category: worker|gate"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    from server.routes._helpers import read_strict_worker_failures

    failures_file = RESULTS_DIR / "worker_failures.jsonl"
    failures = read_strict_worker_failures(
        failures_file,
        results_dir=RESULTS_DIR,
        checkpoint_path=RESULTS_DIR / "pipeline_state.json",
        limit=None,
    )
    failures = [
        entry
        for entry in failures
        if (gen is None or entry.get("gen") == gen)
        and (not role or role.lower() in str(entry.get("role") or "").lower())
        and (not category or entry.get("category") == category)
    ]
    total = len(failures)
    return {"failures": failures[offset:offset + limit], "total": total}
