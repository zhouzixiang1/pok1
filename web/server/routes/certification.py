"""Official platform certification status and queue endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from bot_namespace import bot_name
from official_certification import build_spec, enqueue_certification, queue_snapshot, status_payload


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BOTS_DIR = PROJECT_ROOT / "bots"
DEFAULT_OPPONENT = BOTS_DIR / "national_v76"

router = APIRouter(prefix="/api/certification", tags=["certification"])


def _bot_dir(version: int) -> Path:
    path = BOTS_DIR / bot_name(version)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Bot v{version} not found")
    return path


@router.get("/queue")
async def get_queue():
    return queue_snapshot()


@router.get("/{version}")
async def get_certification(version: int):
    return status_payload(_bot_dir(version))


@router.post("/{version}/enqueue")
async def enqueue(version: int, mode: str = Query("full", pattern="^(smoke|full)$")):
    candidate = _bot_dir(version)
    opponent = DEFAULT_OPPONENT if DEFAULT_OPPONENT.exists() else None
    spec = build_spec(mode, candidate, opponent=opponent)
    return enqueue_certification(spec, reason="api_enqueue")
