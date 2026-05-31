"""Match endpoints — match matrix, stats, replays, and commentary."""

import fcntl
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
STATS_FILE = RESULTS_DIR / "elo_daemon_stats.json"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"
REPLAY_DIR = RESULTS_DIR / "match_replay"
MATCH_HISTORY_FILE = RESULTS_DIR / "match_history.jsonl"

router = APIRouter(prefix="/api", tags=["matches"])


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


@router.get("/matches/matrix")
async def match_matrix():
    # Try H2H data first (win-rate matrix)
    h2h = _cached_read("matches_h2h", H2H_FILE)
    if h2h:
        all_bots = set()
        for k in h2h:
            parts = k.split(" vs ")
            all_bots.update(parts)
        bot_names = sorted(all_bots, key=lambda n: int(re.search(r'\d+', n).group()) if re.search(r'\d+', n) else 0)
        n = len(bot_names)
        wr_matrix = [[None] * n for _ in range(n)]
        for k, v in h2h.items():
            parts = k.split(" vs ")
            if len(parts) != 2:
                continue
            a, b = parts[0].strip(), parts[1].strip()
            if a in bot_names and b in bot_names:
                i, j = bot_names.index(a), bot_names.index(b)
                wr = v.get("win_rate")
                if wr is not None:
                    wr_matrix[i][j] = round(wr, 4)
                    wr_matrix[j][i] = round(1.0 - wr, 4)
        return {"bots": bot_names, "matrix": wr_matrix, "source": "h2h"}

    # Fallback to legacy pair counts
    stats = _cached_read("stats", STATS_FILE)
    if not stats:
        return {"bots": [], "matrix": []}
    ratings = _cached_read("ratings", RATINGS_FILE) or {}
    bot_names = sorted(
        ratings.keys(),
        key=lambda n: int(re.search(r'\d+', n).group()) if re.search(r'\d+', n) else 0
    )
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


@router.get("/matches/stats")
async def match_stats():
    stats = _cached_read("stats", STATS_FILE)
    if not stats:
        return {"total_games": 0, "total_pairs": 0, "total_periods": 0, "most_active_pair": "", "most_active_count": 0}
    pairs = stats.get("pairs", {})
    total_games = stats.get("total_games", sum(pairs.values()))
    most_active = max(pairs.items(), key=lambda x: x[1]) if pairs else ("", 0)
    return {
        "total_games": total_games,
        "total_pairs": len(pairs),
        "total_periods": stats.get("total_periods", 0),
        "most_active_pair": most_active[0],
        "most_active_count": most_active[1],
    }


@router.get("/matches/recent")
async def recent_matches(limit: int = Query(50, le=200)):
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


@router.get("/matches/replay/{match_id}")
async def match_replay(match_id: str):
    path = (REPLAY_DIR / match_id).resolve()
    if not path.is_relative_to(REPLAY_DIR.resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail="Match not found")
    return _read_locked(path)


@router.get("/matches/commentary/{match_id}")
async def match_commentary(match_id: str):
    path = (REPLAY_DIR / match_id).resolve()
    if not path.is_relative_to(REPLAY_DIR.resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail="Match not found")

    COMMENTARY_DIR = RESULTS_DIR / "commentary"
    cache_path = (COMMENTARY_DIR / match_id).resolve()
    if cache_path.is_file() and cache_path.is_relative_to(COMMENTARY_DIR.resolve()):
        try:
            return _read_locked(cache_path)
        except (json.JSONDecodeError, OSError):
            pass

    replay = _read_locked(path)
    from commentary import generate_match_commentary
    commentary = generate_match_commentary(replay)

    try:
        COMMENTARY_DIR.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(commentary, f)
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass

    return commentary
