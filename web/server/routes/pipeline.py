"""Pipeline state endpoints — checkpoint and worker failures."""

from pathlib import Path

from fastapi import APIRouter, Query

from server.routes._helpers import (
    load_strict_pipeline_checkpoint,
    read_strict_worker_failures,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
PIPELINE_STATE_FILE = RESULTS_DIR / "pipeline_state.json"
WORKER_FAILURES_FILE = RESULTS_DIR / "worker_failures.jsonl"

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


@router.get("/checkpoint")
async def pipeline_checkpoint():
    """Return current pipeline checkpoint (stage of in-progress generation)."""
    return load_strict_pipeline_checkpoint(RESULTS_DIR, PIPELINE_STATE_FILE)


@router.get("/failures")
async def pipeline_failures(limit: int = Query(10, le=50)):
    """Return failures explicitly bound to the current strict workflow."""
    return read_strict_worker_failures(
        WORKER_FAILURES_FILE,
        results_dir=RESULTS_DIR,
        checkpoint_path=PIPELINE_STATE_FILE,
        limit=limit,
    )
