"""Scheduler status and result API routes."""

from fastapi import APIRouter

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.get("/status")
async def scheduler_status():
    """Return current scheduler queue status: pending, claimed, recent results counts."""
    from battle_scheduler import BATTLE_JOBS_FILE, BATTLE_CLAIMED_FILE, BATTLE_RESULTS_FILE, _read_jsonl
    pending = _read_jsonl(BATTLE_JOBS_FILE)
    claimed = _read_jsonl(BATTLE_CLAIMED_FILE)
    results = _read_jsonl(BATTLE_RESULTS_FILE)
    return {
        "pending_jobs": len(pending),
        "claimed_jobs": len(claimed),
        "recent_results": len(results),
        "pending_details": pending[:5],  # latest 5
    }


@router.get("/results")
async def scheduler_results(limit: int = 20):
    """Return the most recent scheduler battle results."""
    from battle_scheduler import BATTLE_RESULTS_FILE, _read_jsonl
    results = _read_jsonl(BATTLE_RESULTS_FILE)
    return {"results": results[-limit:] if limit > 0 else []}
