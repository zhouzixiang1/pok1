"""Log endpoints — generation logs browsing and orchestrator logs."""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
ORCHESTRATOR_LOGS_DIR = PROJECT_ROOT / "web" / "logs"

router = APIRouter(prefix="/api", tags=["logs"])


@router.get("/logs/generations")
async def list_generations():
    if not RESULTS_DIR.exists():
        return []
    versions = []
    dirs = sorted(
        (p for p in RESULTS_DIR.iterdir()
         if p.is_dir() and p.name.startswith("v") and (p / "logs").is_dir()),
        key=lambda p: int(re.search(r'\d+', p.name).group()) if re.search(r'\d+', p.name) else 0,
    )
    for p in dirs:
        files = sorted(f.name for f in (p / "logs").iterdir() if f.is_file())
        versions.append({"version": p.name, "files": files})
    return versions


@router.get("/logs/generations/{version}/{filename}")
async def get_log(version: str, filename: str, tail: int = Query(0)):
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
async def get_orchestrator_log(filename: str, tail: int = Query(0)):
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
