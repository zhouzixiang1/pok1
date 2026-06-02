"""Rating endpoints — Glicko-2 ratings and history."""

import json
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from server.cache import cached_read
from server.routes._helpers import (
    build_rating_row, build_ranked_ratings, downsample, read_jsonl,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
EXPERIENCE_FILE = PROJECT_ROOT / "web" / "core" / "experience_pool.md"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
STATS_FILE = RESULTS_DIR / "elo_daemon_stats.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"
BOT_STATS_FILE = RESULTS_DIR / "bot_stats.json"
HISTORY_FILE = RESULTS_DIR / "rating_history.jsonl"

router = APIRouter(prefix="/api", tags=["ratings"])


@router.get("/ratings")
async def get_ratings():
    data = cached_read("ratings", RATINGS_FILE)
    bot_stats_data = cached_read("bot_stats", BOT_STATS_FILE) or {}
    h2h_data = cached_read("h2h", H2H_FILE) or {}
    return build_ranked_ratings(data or {}, bot_stats_data, h2h_data)


@router.get("/ratings/{bot_name}")
async def get_rating_detail(bot_name: str):
    data = cached_read("ratings", RATINGS_FILE)
    bot_stats_data = cached_read("bot_stats", BOT_STATS_FILE) or {}
    h2h_data = cached_read("h2h", H2H_FILE) or {}
    if not data or bot_name not in data:
        raise HTTPException(status_code=404, detail="Bot not found")
    return build_rating_row(bot_name, data[bot_name], bot_stats_data, h2h_data)


@router.get("/history")
async def history(
    bots: str = Query("", description="Comma-separated bot names"),
    resolution: str = Query("medium", description="full, medium, or low"),
):
    entries = read_jsonl(HISTORY_FILE, reverse=False)
    if resolution != "full" and len(entries) > 100:
        step = max(1, len(entries) // (200 if resolution == "medium" else 50))
        sampled = entries[::step]
        if entries[-1] not in sampled:
            sampled.append(entries[-1])
        entries = sampled
    bot_filter = set(b.strip() for b in bots.split(",") if b.strip()) if bots else None

    result = []
    for entry in entries:
        ratings = entry.get("ratings", {})
        win_rates = entry.get("win_rates", {})
        if bot_filter:
            ratings = {k: v for k, v in ratings.items() if k in bot_filter}
            win_rates = {k: v for k, v in win_rates.items() if k in bot_filter}
        result.append({
            "period": entry.get("period", 0),
            "timestamp": entry.get("timestamp", ""),
            "ratings": ratings,
            "win_rates": win_rates,
        })
    return result


@router.get("/history/summary")
async def history_summary():
    entries = read_jsonl(HISTORY_FILE, reverse=False)
    if not entries:
        return {}
    all_bots = set()
    for e in entries:
        all_bots.update(e.get("ratings", {}).keys())
    summary = {}
    for bot in sorted(all_bots):
        s = {}
        ratings = []
        for e in entries:
            bot_r = e.get("ratings", {}).get(bot)
            if not isinstance(bot_r, dict):
                continue
            r = bot_r.get("r")
            if r is not None:
                ratings.append(r)
        if ratings:
            s["peak_rating"] = round(max(ratings), 1)
            s["current_rating"] = round(ratings[-1], 1)
            s["trend"] = round(ratings[-1] - ratings[0], 1) if len(ratings) > 1 else 0
            s["periods"] = len(ratings)
        wr_list = [
            e["win_rates"][bot]["h2h_avg_wr"]
            for e in entries
            if bot in e.get("win_rates", {}) and e["win_rates"][bot].get("h2h_avg_wr") is not None
        ]
        if wr_list:
            s["peak_h2h_avg_wr"] = round(max(wr_list), 4)
            s["current_h2h_avg_wr"] = round(wr_list[-1], 4)
            s["wr_trend"] = round(wr_list[-1] - wr_list[0], 4) if len(wr_list) > 1 else 0
        if s:
            summary[bot] = s
    return summary


@router.get("/experience", response_class=PlainTextResponse)
async def experience():
    if not EXPERIENCE_FILE.exists():
        return ""
    from evolution_infra import locked_file
    with locked_file(EXPERIENCE_FILE, "r") as f:
        return f.read()


class ExperienceUpdateRequest(BaseModel):
    content: str


class ExperienceAppendRequest(BaseModel):
    lesson: str


@router.put("/experience")
async def update_experience(req: ExperienceUpdateRequest):
    """Overwrite experience_pool.md with new content."""
    try:
        from evolution_infra import locked_file
        with locked_file(EXPERIENCE_FILE, "w", encoding="utf-8") as f:
            f.write(req.content)
        lines = req.content.count("\n") + 1
        return {"saved": True, "lines": lines, "chars": len(req.content)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/experience/append")
async def append_experience(req: ExperienceAppendRequest):
    """Append a new lesson to experience_pool.md."""
    lesson = req.lesson.strip()
    if not lesson:
        raise HTTPException(status_code=400, detail="lesson is empty")
    try:
        import fcntl
        with open(EXPERIENCE_FILE, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                existing = f.read()
                separator = "\n\n" if existing and not existing.endswith("\n\n") else ""
                new_content = existing + separator + f"- {lesson}\n"
                f.seek(0)
                f.truncate()
                f.write(new_content)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return {"appended": True, "lesson": lesson, "total_chars": len(new_content)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
    data = cached_read("h2h", H2H_FILE)
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
    data = cached_read("bot_stats", BOT_STATS_FILE)
    return data or {}
