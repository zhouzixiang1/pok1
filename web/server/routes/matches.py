"""Match endpoints — match matrix, stats, replays, and commentary."""

import fcntl
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from server.cache import cached_read, read_locked
from server.routes._helpers import build_match_matrix, build_match_stats, read_jsonl

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
STATS_FILE = RESULTS_DIR / "elo_daemon_stats.json"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"
REPLAY_DIR = RESULTS_DIR / "match_replay"
MATCH_HISTORY_FILE = RESULTS_DIR / "match_history.jsonl"

router = APIRouter(prefix="/api", tags=["matches"])


@router.get("/matches/matrix")
async def match_matrix():
    h2h = cached_read("matches_h2h", H2H_FILE)
    ratings = cached_read("ratings", RATINGS_FILE)
    stats = cached_read("stats", STATS_FILE)
    return build_match_matrix(h2h, ratings, stats)


@router.get("/matches/stats")
async def match_stats():
    stats = cached_read("stats", STATS_FILE)
    return build_match_stats(stats)


@router.get("/matches/recent")
async def recent_matches(limit: int = Query(50, le=200)):
    return read_jsonl(MATCH_HISTORY_FILE, limit=limit)


@router.get("/matches/replay/{match_id}")
async def match_replay(match_id: str):
    path = (REPLAY_DIR / match_id).resolve()
    if not path.is_relative_to(REPLAY_DIR.resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail="Match not found")
    return read_locked(path)


@router.get("/matches/commentary/{match_id}")
async def match_commentary(match_id: str):
    path = (REPLAY_DIR / match_id).resolve()
    if not path.is_relative_to(REPLAY_DIR.resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail="Match not found")

    COMMENTARY_DIR = RESULTS_DIR / "commentary"
    cache_path = (COMMENTARY_DIR / match_id).resolve()
    if cache_path.is_file() and cache_path.is_relative_to(COMMENTARY_DIR.resolve()):
        try:
            return read_locked(cache_path)
        except (json.JSONDecodeError, OSError):
            pass

    replay = read_locked(path)
    from commentary import generate_match_commentary
    import asyncio
    commentary = await asyncio.get_running_loop().run_in_executor(None, generate_match_commentary, replay)

    try:
        COMMENTARY_DIR.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(commentary, f)
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass

    return commentary
