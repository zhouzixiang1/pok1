"""Official platform certification status and queue endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from bot_namespace import bot_name
from official_certification import (
    build_spec,
    select_official_opponent,
    status_payload,
)
from official_certification_job import (
    cancel_job,
    get_job,
    job_snapshot,
    start_or_poll_job,
)
from blocking_runtime import run_blocking_isolated


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BOTS_DIR = PROJECT_ROOT / "bots"

router = APIRouter(prefix="/api/certification", tags=["certification"])


def _bot_dir(version: int) -> Path:
    path = BOTS_DIR / bot_name(version)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Bot v{version} not found")
    return path


@router.get("/queue")
async def get_queue():
    return job_snapshot()


@router.get("/jobs")
async def get_jobs():
    return job_snapshot()


@router.get("/jobs/{job_id}")
async def get_certification_job(job_id: str):
    payload = get_job(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Official certification job not found")
    return payload


@router.post("/jobs/{job_id}/cancel")
async def cancel_certification_job(job_id: str):
    payload = await run_blocking_isolated(
        cancel_job,
        job_id,
        reason="api_cancelled",
        thread_name_prefix="official-api",
    )
    if payload.get("state") == "missing":
        raise HTTPException(status_code=404, detail="Official certification job not found")
    return payload


@router.get("/{version}")
async def get_certification(version: int):
    return status_payload(_bot_dir(version))


@router.post("/{version}/enqueue")
async def enqueue(version: int, mode: str = Query("compliance", pattern="^(smoke|compliance|full)$")):
    candidate = _bot_dir(version)
    selection = select_official_opponent(candidate, preferred=None)
    if not selection.get("selected"):
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "no_official_eligible_opponent",
                "opponent_selection": selection,
            },
        )
    opponent = selection["opponent"]["path"]
    spec = build_spec(mode, candidate, opponent=opponent)
    payload = await run_blocking_isolated(
        start_or_poll_job,
        spec,
        thread_name_prefix="official-api",
        opponent_selection=selection,
        source_v=None,
    )
    return {
        **payload,
        "status": (
            (payload.get("status") or {}).get("status")
            if isinstance(payload.get("status"), dict)
            else "official-pending"
        ),
        "queued": payload.get("state") == "queued",
        "mode": mode,
        "opponent_selection": selection,
    }
