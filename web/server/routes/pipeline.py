"""Pipeline state endpoints — checkpoint and worker failures."""

import json
from pathlib import Path

from fastapi import APIRouter, Query

from server.routes._helpers import read_jsonl

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
PIPELINE_STATE_FILE = RESULTS_DIR / "pipeline_state.json"
WORKER_FAILURES_FILE = RESULTS_DIR / "worker_failures.jsonl"

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


@router.get("/checkpoint")
async def pipeline_checkpoint():
    """Return current pipeline checkpoint (stage of in-progress generation)."""
    if not PIPELINE_STATE_FILE.exists():
        return None
    try:
        from evolution_infra import locked_file
        with locked_file(PIPELINE_STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None


@router.get("/failures")
async def pipeline_failures(limit: int = Query(10, le=50)):
    """Return recent worker failures from worker_failures.jsonl."""
    entries = read_jsonl(WORKER_FAILURES_FILE, limit=limit)
    return entries
