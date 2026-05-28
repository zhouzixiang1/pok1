"""Log endpoints — generation logs browsing."""

import re
from pathlib import Path

from fastapi import APIRouter, Query

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"

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
    path = RESULTS_DIR / version / "logs" / filename
    if not path.is_file():
        return {"version": version, "filename": filename, "content": ""}
    with open(path, "r") as f:
        if tail > 0:
            lines = f.readlines()
            content = "".join(lines[-tail:])
        else:
            content = f.read()
    return {"version": version, "filename": filename, "content": content}
