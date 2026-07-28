"""Log endpoints — generation logs browsing, orchestrator logs, system events, and worker failures."""

import json
import os
import re
import stat
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from blocking_runtime import run_blocking_isolated
from evolution_infra import locked_file

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
ORCHESTRATOR_LOGS_DIR = PROJECT_ROOT / "web" / "logs"

router = APIRouter(prefix="/api", tags=["logs"])


def _read_generation_log(
    results_dir: Path,
    *,
    version: str,
    identifier: str,
    tail: int,
) -> str:
    """Read through a no-follow descriptor walk rooted at RESULTS_DIR.

    Resolving a safe Path and opening it later is insufficient: any parent can
    be renamed and replaced by a symlink between those operations.  Each path
    component is therefore opened relative to the already verified parent
    descriptor, so a concurrent rename can at most leave this read anchored to
    the original in-tree directory; it can never redirect the read elsewhere.
    """

    from server.routes._helpers import generation_log_parts

    parts = generation_log_parts(identifier)
    if parts is None:
        raise ValueError("invalid generation log identity")
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    file_flags |= getattr(os, "O_NOFOLLOW", 0)
    directories: list[int] = []
    descriptor = -1
    try:
        current = os.open(results_dir, directory_flags)
        directories.append(current)
        for component in (version, "logs", *parts[:-1]):
            current = os.open(component, directory_flags, dir_fd=current)
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                os.close(current)
                raise OSError("generation log parent is not a directory")
            directories.append(current)
        descriptor = os.open(parts[-1], file_flags, dir_fd=current)
        with os.fdopen(
            descriptor,
            "r",
            encoding="utf-8",
            errors="replace",
        ) as handle:
            descriptor = -1
            opened = os.fstat(handle.fileno())
            live = os.stat(
                parts[-1],
                dir_fd=current,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(live.st_mode)
                or opened.st_nlink != 1
                or live.st_nlink != 1
                or (opened.st_dev, opened.st_ino)
                != (live.st_dev, live.st_ino)
            ):
                raise OSError("generation log path is not the opened file")
            if tail > 0:
                content = "".join(handle.readlines()[-tail:])
            else:
                content = handle.read()
            finished = os.fstat(handle.fileno())
            live_after = os.stat(
                parts[-1],
                dir_fd=current,
                follow_symlinks=False,
            )
            if (
                finished.st_nlink != 1
                or live_after.st_nlink != 1
                or (finished.st_dev, finished.st_ino)
                != (live_after.st_dev, live_after.st_ino)
            ):
                raise OSError("generation log changed identity during read")
            return content
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        for directory in reversed(directories):
            os.close(directory)


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


def _list_generations_blocking() -> list:
    from server.routes._helpers import (
        list_generation_dirs,
        strict_observable_generation_versions,
    )

    allowed = strict_observable_generation_versions(
        RESULTS_DIR,
        RESULTS_DIR / "pipeline_state.json",
    )
    return list_generation_dirs(RESULTS_DIR, allowed_versions=allowed)


@router.get("/logs/generations")
async def list_generations():
    """List observable generation log directories for the current strict cycle.

    Offloaded to an isolated worker thread: the observable-versions read and
    ``list_generation_dirs`` perform blocking directory walks / JSON reads that
    must not freeze the shared uvicorn event loop.
    """
    return await run_blocking_isolated(
        _list_generations_blocking,
        thread_name_prefix="logs-generations",
    )


def _get_generation_log_blocking(version: str, filename: str, tail: int) -> dict:
    from server.routes._helpers import (
        generation_log_path,
        strict_observable_generation_versions,
    )

    if re.fullmatch(r"v[1-9][0-9]*", version) is None:
        raise HTTPException(status_code=400, detail="Invalid generation")
    allowed = strict_observable_generation_versions(
        RESULTS_DIR,
        RESULTS_DIR / "pipeline_state.json",
    )
    if int(version[1:]) not in allowed:
        raise HTTPException(status_code=404, detail="Generation not observable")
    path = generation_log_path(RESULTS_DIR / version / "logs", filename)
    if path is None:
        raise HTTPException(status_code=400, detail="Invalid log identity")
    try:
        content = _read_generation_log(
            RESULTS_DIR,
            version=version,
            identifier=filename,
            tail=tail,
        )
    except FileNotFoundError:
        return {"version": version, "filename": filename, "content": ""}
    except (OSError, UnicodeError) as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Log unavailable: {type(exc).__name__}",
        ) from exc
    return {"version": version, "filename": filename, "content": content}


@router.get("/logs/generations/{version}/{filename}")
async def get_log(version: str, filename: str, tail: int = Query(0, ge=0)):
    """Read a generation log file under a no-follow descriptor walk.

    Offloaded to an isolated worker thread: the observable-versions read, the
    descriptor walk, and the (potentially multi-MB) file read are blocking
    operations that must not freeze the shared uvicorn event loop.
    """
    return await run_blocking_isolated(
        _get_generation_log_blocking,
        version,
        filename,
        tail,
        thread_name_prefix="logs-generations",
    )


def _list_orchestrator_logs_blocking() -> list:
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


@router.get("/logs/orchestrator")
async def list_orchestrator_logs():
    """List orchestrator log files (most recent first).

    Offloaded to an isolated worker thread: iterdir + stat on every file under
    the logs dir are blocking operations that must not freeze the shared
    uvicorn event loop (single-worker, shared with the orchestrator).
    """
    return await run_blocking_isolated(
        _list_orchestrator_logs_blocking,
        thread_name_prefix="logs-orchestrator",
    )


def _get_orchestrator_log_blocking(filename: str, tail: int) -> tuple[int, str]:
    """Return ``(status_code, content)`` for the orchestrator-log read.

    Runs on an isolated worker thread (see ``get_orchestrator_log``) so the
    potentially multi-MB ``read_text`` never freezes the event loop. The
    status/message precedence matches the original synchronous handler exactly.
    """
    if not filename.startswith("orchestrator_") or not filename.endswith(".txt") or "/" in filename:
        return 400, "Invalid filename"
    if _current_log_epoch_identity() is None:
        return 404, "Log epoch unavailable"
    path = ORCHESTRATOR_LOGS_DIR / filename
    if path.is_symlink() or not path.is_file():
        return 404, "File not found"
    content = path.read_text(errors="replace")
    if tail > 0:
        lines = content.splitlines()
        content = "\n".join(lines[-tail:])
    return 200, content


@router.get("/logs/orchestrator/{filename}", response_class=PlainTextResponse)
async def get_orchestrator_log(filename: str, tail: int = Query(0, ge=0)):
    """Get orchestrator log content. filename must be orchestrator_*.txt.

    Offloaded to an isolated worker thread: the (potentially multi-MB) log
    ``read_text`` must not freeze the shared uvicorn event loop.
    """
    status_code, content = await run_blocking_isolated(
        _get_orchestrator_log_blocking,
        filename,
        tail,
        thread_name_prefix="logs-orchestrator",
    )
    return PlainTextResponse(content, status_code=status_code)


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


def _get_system_events_blocking(
    type: str,
    category: str,
    severity: str,
    source: str,
    run_id: str,
    stage: str,
    since: float | None,
    limit: int,
    offset: int,
) -> dict:
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
    """Structured system events for the current policy epoch.

    Offloaded to an isolated worker thread: parsing every line of
    ``events.jsonl`` under flock is blocking work that must not freeze the
    shared uvicorn event loop (single-worker, shared with the orchestrator).
    """
    return await run_blocking_isolated(
        _get_system_events_blocking,
        type,
        category,
        severity,
        source,
        run_id,
        stage,
        since,
        limit,
        offset,
        thread_name_prefix="logs-system-events",
    )


def _get_worker_failures_blocking(
    gen: int | None,
    role: str,
    category: str,
    limit: int,
    offset: int,
) -> dict:
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


@router.get("/logs/worker-failures")
async def get_worker_failures(
    gen: int = Query(None, description="Filter by generation number"),
    role: str = Query("", description="Filter by role name"),
    category: str = Query("", description="Filter by category: worker|gate"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Strict worker/gate failure records for the current evaluation cycle.

    Offloaded to an isolated worker thread: ``read_strict_worker_failures``
    reads the entire ``worker_failures.jsonl`` and is blocking work that must
    not freeze the shared uvicorn event loop.
    """
    return await run_blocking_isolated(
        _get_worker_failures_blocking,
        gen,
        role,
        category,
        limit,
        offset,
        thread_name_prefix="logs-worker-failures",
    )
