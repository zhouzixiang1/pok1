"""Rating endpoints — Glicko-2 ratings and history."""

import fcntl
import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
EXPERIENCE_FILE = PROJECT_ROOT / "web" / "core" / "experience_pool.md"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
STATS_FILE = RESULTS_DIR / "elo_daemon_stats.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"
BOT_STATS_FILE = RESULTS_DIR / "bot_stats.json"
HISTORY_FILE = RESULTS_DIR / "rating_history.jsonl"

router = APIRouter(prefix="/api", tags=["ratings"])

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 2.0


def _read_locked(path: Path) -> Any:
    with open(path, "r") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        data = json.load(f)
        fcntl.flock(f, fcntl.LOCK_UN)
    return data


def _cached_read(key: str, path: Path) -> Any:
    now = time.time()
    if key in _cache:
        mtime, data = _cache[key]
        if now - mtime < _CACHE_TTL:
            return data
    if not path.exists():
        return None
    data = _read_locked(path)
    _cache[key] = (now, data)
    return data


def _confidence(rd: float) -> str:
    if rd < 50:
        return "very_confident"
    if rd < 100:
        return "confident"
    if rd < 200:
        return "uncertain"
    return "very_uncertain"


@router.get("/ratings")
async def get_ratings():
    data = _cached_read("ratings", RATINGS_FILE)
    if not data:
        return []
    rows = []
    for name, d in data.items():
        r, rd = d["r"], d["rd"]
        rows.append({
            "name": name,
            "rating": round(r, 1),
            "rd": round(rd, 1),
            "sigma": round(d.get("sigma", 0.06), 4),
            "conservative_rating": round(r - 2 * rd, 1),
            "confidence": _confidence(rd),
            "last_period": d.get("last_period", ""),
        })
    rows.sort(key=lambda x: x["conservative_rating"], reverse=True)
    for i, row in enumerate(rows):
        row["rank"] = i + 1
    return rows


@router.get("/ratings/{bot_name}")
async def get_rating_detail(bot_name: str):
    data = _cached_read("ratings", RATINGS_FILE)
    if not data or bot_name not in data:
        return {"error": "Bot not found"}
    d = data[bot_name]
    r, rd = d["r"], d["rd"]
    return {
        "name": bot_name,
        "rating": round(r, 1),
        "rd": round(rd, 1),
        "sigma": round(d.get("sigma", 0.06), 4),
        "conservative_rating": round(r - 2 * rd, 1),
        "confidence": _confidence(rd),
        "last_period": d.get("last_period", ""),
    }


def _load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    entries = []
    with open(HISTORY_FILE, "r") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
        fcntl.flock(f, fcntl.LOCK_UN)
    return entries


def _downsample(entries: list[dict], resolution: str) -> list[dict]:
    if resolution == "full" or len(entries) <= 100:
        return entries
    step = max(1, len(entries) // (200 if resolution == "medium" else 50))
    sampled = entries[::step]
    if entries[-1] not in sampled:
        sampled.append(entries[-1])
    return sampled


@router.get("/history")
async def history(
    bots: str = Query("", description="Comma-separated bot names"),
    resolution: str = Query("medium", description="full, medium, or low"),
):
    entries = _load_history()
    entries = _downsample(entries, resolution)
    bot_filter = set(b.strip() for b in bots.split(",") if b.strip()) if bots else None

    result = []
    for entry in entries:
        ratings = entry.get("ratings", {})
        if bot_filter:
            ratings = {k: v for k, v in ratings.items() if k in bot_filter}
        result.append({
            "period": entry.get("period", 0),
            "timestamp": entry.get("timestamp", ""),
            "ratings": ratings,
        })
    return result


@router.get("/history/summary")
async def history_summary():
    entries = _load_history()
    if not entries:
        return {}
    all_bots = set()
    for e in entries:
        all_bots.update(e.get("ratings", {}).keys())
    summary = {}
    for bot in sorted(all_bots):
        ratings = [e["ratings"][bot]["r"] for e in entries if bot in e.get("ratings", {})]
        if ratings:
            summary[bot] = {
                "peak_rating": round(max(ratings), 1),
                "current_rating": round(ratings[-1], 1),
                "trend": round(ratings[-1] - ratings[0], 1) if len(ratings) > 1 else 0,
                "periods": len(ratings),
            }
    return summary


@router.get("/experience", response_class=PlainTextResponse)
async def experience():
    if not EXPERIENCE_FILE.exists():
        return ""
    return EXPERIENCE_FILE.read_text()


class ExperienceUpdateRequest(BaseModel):
    content: str


class ExperienceAppendRequest(BaseModel):
    lesson: str


@router.put("/experience")
async def update_experience(req: ExperienceUpdateRequest):
    """Overwrite experience_pool.md with new content."""
    try:
        EXPERIENCE_FILE.write_text(req.content, encoding="utf-8")
        lines = req.content.count("\n") + 1
        return {"saved": True, "lines": lines, "chars": len(req.content)}
    except Exception as e:
        return {"error": str(e)}


@router.post("/experience/append")
async def append_experience(req: ExperienceAppendRequest):
    """Append a new lesson to experience_pool.md."""
    lesson = req.lesson.strip()
    if not lesson:
        return {"error": "lesson is empty"}
    try:
        existing = EXPERIENCE_FILE.read_text(encoding="utf-8") if EXPERIENCE_FILE.exists() else ""
        separator = "\n\n" if existing and not existing.endswith("\n\n") else ""
        new_content = existing + separator + f"- {lesson}\n"
        EXPERIENCE_FILE.write_text(new_content, encoding="utf-8")
        return {"appended": True, "lesson": lesson, "total_chars": len(new_content)}
    except Exception as e:
        return {"error": str(e)}


@router.get("/daemon/status")
async def daemon_status():
    import os
    from server.state import app_state
    config = app_state.get_config()
    if RATINGS_FILE.exists():
        mtime = os.path.getmtime(RATINGS_FILE)
        age = time.time() - mtime
        status = "active" if age < 60 else ("recent" if age < 600 else "idle")
        return {"status": status, "last_update_age_seconds": round(age, 0), "daemon_enabled": config["daemon_enabled"]}
    return {"status": "unknown", "last_update_age_seconds": -1, "daemon_enabled": config["daemon_enabled"]}


@router.get("/h2h")
async def get_h2h(bot_name: str = Query("", description="Filter by bot name")):
    data = _cached_read("h2h", H2H_FILE)
    if not data:
        return {}
    if not bot_name:
        return data
    filtered = {}
    for k, v in data.items():
        parts = k.split(" vs ")
        if bot_name in parts:
            filtered[k] = v
    return filtered


@router.get("/bot-stats")
async def get_all_bot_stats():
    data = _cached_read("bot_stats", BOT_STATS_FILE)
    return data or {}
