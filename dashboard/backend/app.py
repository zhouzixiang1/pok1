"""FastAPI backend for Evolution Dashboard."""

import asyncio
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import fcntl
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

# ── Paths ──
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "evolution_workspace" / "results"
EXPERIENCE_FILE = PROJECT_ROOT / "evolution_workspace" / "experience_pool.md"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
STATS_FILE = RESULTS_DIR / "elo_daemon_stats.json"
HISTORY_FILE = RESULTS_DIR / "rating_history.jsonl"

app = FastAPI(title="Evolution Dashboard API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Data helpers ──

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


# ── Ratings ──

@app.get("/api/ratings")
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


@app.get("/api/ratings/{bot_name}")
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


@app.get("/api/ratings/stream")
async def ratings_stream():
    async def generate():
        while True:
            data = _cached_read("ratings", RATINGS_FILE)
            if data:
                rows = []
                for name, d in data.items():
                    rows.append({"name": name, "rating": round(d["r"], 1), "rd": round(d["rd"], 1)})
                yield {"event": "ratings", "data": json.dumps(rows)}
            await asyncio.sleep(2)
    return EventSourceResponse(generate())


# ── History ──

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


@app.get("/api/history")
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


@app.get("/api/history/summary")
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


# ── Matches ──

@app.get("/api/matches/matrix")
async def match_matrix():
    stats = _cached_read("stats", STATS_FILE)
    if not stats:
        return {"bots": [], "matrix": []}
    ratings = _cached_read("ratings", RATINGS_FILE) or {}
    bot_names = sorted(ratings.keys(), key=lambda n: int(re.search(r'\d+', n).group()) if re.search(r'\d+', n) else 0)
    n = len(bot_names)
    matrix = [[0] * n for _ in range(n)]
    pairs = stats.get("pairs", {})
    for key, count in pairs.items():
        parts = key.split(" vs ")
        if len(parts) == 2:
            a, b = parts[0].strip(), parts[1].strip()
            if a in bot_names and b in bot_names:
                i, j = bot_names.index(a), bot_names.index(b)
                matrix[i][j] = count
                matrix[j][i] = count
    return {"bots": bot_names, "matrix": matrix}


@app.get("/api/matches/stats")
async def match_stats():
    stats = _cached_read("stats", STATS_FILE)
    if not stats:
        return {"total_games": 0, "total_pairs": 0, "total_periods": 0, "most_active_pair": ""}
    pairs = stats.get("pairs", {})
    total_games = sum(pairs.values()) * 50
    most_active = max(pairs.items(), key=lambda x: x[1]) if pairs else ("", 0)
    return {
        "total_games": total_games,
        "total_pairs": len(pairs),
        "total_periods": stats.get("total_periods", 0),
        "most_active_pair": most_active[0],
        "most_active_count": most_active[1],
    }


# ── Experience ──

@app.get("/api/experience", response_class=PlainTextResponse)
async def experience():
    if not EXPERIENCE_FILE.exists():
        return ""
    return EXPERIENCE_FILE.read_text()


# ── Logs ──

@app.get("/api/logs/generations")
async def list_generations():
    if not RESULTS_DIR.exists():
        return []
    versions = []
    for p in sorted(RESULTS_DIR.iterdir()):
        if p.is_dir() and p.name.startswith("v") and (p / "logs").is_dir():
            files = sorted(f.name for f in (p / "logs").iterdir() if f.is_file())
            versions.append({"version": p.name, "files": files})
    return versions


@app.get("/api/logs/generations/{version}/{filename}")
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


# ── Daemon Status ──

@app.get("/api/daemon/status")
async def daemon_status():
    if RATINGS_FILE.exists():
        mtime = os.path.getmtime(RATINGS_FILE)
        age = time.time() - mtime
        status = "active" if age < 60 else ("recent" if age < 600 else "idle")
        return {"status": status, "last_update_age_seconds": round(age, 0)}
    return {"status": "unknown", "last_update_age_seconds": -1}


# ── Static files (production) ──

STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve the SPA index.html for all non-API routes."""
        return FileResponse(STATIC_DIR / "index.html")
