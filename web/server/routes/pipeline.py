"""Pipeline state endpoints — checkpoint and worker failures."""

import fcntl
import json
from pathlib import Path

from fastapi import APIRouter, Query

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
        with open(PIPELINE_STATE_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
        return data
    except Exception:
        return None


@router.get("/failures")
async def pipeline_failures(limit: int = Query(10, le=50)):
    """Return recent worker failures from worker_failures.jsonl."""
    if not WORKER_FAILURES_FILE.exists():
        return []
    entries = []
    try:
        with open(WORKER_FAILURES_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        return []
    # Return most recent first
    entries.reverse()
    return entries[:limit]
