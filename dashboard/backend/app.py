"""FastAPI backend for Evolution Dashboard — with integrated evolution loop."""

import asyncio
import json
import os
import re
import sys
import time
import threading
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

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
REPLAY_DIR = RESULTS_DIR / "match_replay"
MATCH_HISTORY_FILE = RESULTS_DIR / "match_history.jsonl"

# ── Evolution integration ──
EVOLUTION_DIR = PROJECT_ROOT / "evolution_workspace"
BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(EVOLUTION_DIR.resolve()))

from web_ui import EventBroadcaster, WebUI

# Global broadcaster + UI (shared across lifespan and endpoints)
broadcaster = EventBroadcaster(buffer_size=500)
web_ui = WebUI(broadcaster)

_evolution_task: Optional[asyncio.Task] = None
_daemon_monitor_stop: Optional[threading.Event] = None
_evolution_disabled = os.environ.get("EVOLUTION_DISABLED", "0") == "1"


_use_orchestrator = os.environ.get("USE_ORCHESTRATOR", "0") == "1"

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start evolution as a background asyncio task, clean up on shutdown."""
    global _evolution_task, _daemon_monitor_stop

    if not _evolution_disabled:
        if _use_orchestrator:
            # ── Orchestrator mode (LLM-driven) ──
            from orchestrator.orchestrator import orchestrator_loop
            os.makedirs(PROJECT_ROOT / "evolution_workspace" / "prompts", exist_ok=True)
            os.makedirs(PROJECT_ROOT / "evolution_workspace" / "results", exist_ok=True)

            _evolution_task = asyncio.create_task(
                orchestrator_loop(web_ui, no_daemon=False)
            )
            web_ui.log_history("🔥 Orchestrator started (LLM-driven mode)", "success")
        else:
            # ── Classic mode (hardcoded main_loop) ──
            from evolution_core import (
                main_loop, start_daemon, stop_daemon,
                daemon_monitor_thread, PROMPTS_DIR, RESULTS_DIR as EVO_RESULTS_DIR,
            )
            os.makedirs(PROMPTS_DIR, exist_ok=True)
            os.makedirs(EVO_RESULTS_DIR, exist_ok=True)

            # Start daemon subprocess
            start_daemon(
                workers=int(os.environ.get("DAEMON_WORKERS", "14")),
                pairs=int(os.environ.get("DAEMON_PAIRS", "5")),
            )

            # Daemon monitor thread
            _daemon_monitor_stop = threading.Event()
            monitor = threading.Thread(
                target=daemon_monitor_thread,
                args=(web_ui, _daemon_monitor_stop),
                daemon=True,
            )
            monitor.start()

            # Launch evolution main_loop as background task
            _evolution_task = asyncio.create_task(
                main_loop(web_ui, is_text_ui=False, no_daemon=False)
            )
            web_ui.log_history("Evolution started (integrated mode)", "success")

    yield  # Application runs

    # ── Shutdown ──
    if _evolution_task and not _evolution_task.done():
        _evolution_task.cancel()
        try:
            await asyncio.wait_for(_evolution_task, timeout=10)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    if _daemon_monitor_stop:
        _daemon_monitor_stop.set()

    if not _evolution_disabled:
        from evolution_core import stop_daemon
        stop_daemon()
        web_ui.log_history("Evolution stopped.", "info")


app = FastAPI(title="Evolution Dashboard API", version="1.0", lifespan=lifespan)

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
        try:
            while True:
                data = _cached_read("ratings", RATINGS_FILE)
                if data:
                    rows = []
                    for name, d in data.items():
                        rows.append({"name": name, "rating": round(d["r"], 1), "rd": round(d["rd"], 1)})
                    yield {"event": "ratings", "data": json.dumps(rows)}
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass
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
    dirs = sorted(
        (p for p in RESULTS_DIR.iterdir() if p.is_dir() and p.name.startswith("v") and (p / "logs").is_dir()),
        key=lambda p: int(re.search(r'\d+', p.name).group()) if re.search(r'\d+', p.name) else 0,
    )
    for p in dirs:
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


# ── Match Replay ──

@app.get("/api/matches/recent")
async def recent_matches(limit: int = Query(50, le=200)):
    """Recent match list from match_history.jsonl."""
    if not MATCH_HISTORY_FILE.exists():
        return []
    entries = []
    with open(MATCH_HISTORY_FILE, "r") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        fcntl.flock(f, fcntl.LOCK_UN)
    entries.reverse()
    return entries[:limit]


@app.get("/api/matches/replay/{match_id}")
async def match_replay(match_id: str):
    """Full replay data for a specific match."""
    path = (REPLAY_DIR / match_id).resolve()
    if not path.is_relative_to(REPLAY_DIR.resolve()) or not path.is_file():
        return {"error": "Match not found"}
    return _read_locked(path)


@app.get("/api/matches/commentary/{match_id}")
async def match_commentary(match_id: str):
    """Generate per-game commentary for a match replay."""
    path = (REPLAY_DIR / match_id).resolve()
    if not path.is_relative_to(REPLAY_DIR.resolve()) or not path.is_file():
        return {"error": "Match not found"}

    # Check cache
    COMMENTARY_DIR = RESULTS_DIR / "commentary"
    cache_path = (COMMENTARY_DIR / match_id).resolve()
    if cache_path.is_file() and cache_path.is_relative_to(COMMENTARY_DIR.resolve()):
        try:
            with open(cache_path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    replay = _read_locked(path)
    from commentary import generate_match_commentary
    commentary = generate_match_commentary(replay)

    # Cache result
    try:
        COMMENTARY_DIR.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(commentary, f)
    except OSError:
        pass

    return commentary


# ── Daemon Status ──

@app.get("/api/daemon/status")
async def daemon_status():
    if RATINGS_FILE.exists():
        mtime = os.path.getmtime(RATINGS_FILE)
        age = time.time() - mtime
        status = "active" if age < 60 else ("recent" if age < 600 else "idle")
        return {"status": status, "last_update_age_seconds": round(age, 0)}
    return {"status": "unknown", "last_update_age_seconds": -1}


# ── Evolution SSE Stream ──

@app.get("/api/evolution/stream")
async def evolution_stream():
    """SSE endpoint for real-time evolution events."""
    cid, queue = broadcaster.add_client()

    async def generate():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield event
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
        except asyncio.CancelledError:
            pass
        finally:
            broadcaster.remove_client(cid)

    return EventSourceResponse(generate())


@app.get("/api/evolution/state")
async def evolution_state():
    """Current state snapshot for initial load."""
    return web_ui.get_state()


# ── Static files (production) ──

STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve the SPA index.html for all non-API routes."""
        return FileResponse(STATIC_DIR / "index.html")
