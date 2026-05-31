"""Global data SSE stream — pushes all dashboard data on scheduled intervals."""

import asyncio
import fcntl
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BOTS_DIR = PROJECT_ROOT / "bots"
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
STATS_FILE = RESULTS_DIR / "elo_daemon_stats.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"
BOT_STATS_FILE = RESULTS_DIR / "bot_stats.json"
HISTORY_FILE = RESULTS_DIR / "rating_history.jsonl"
MATCH_HISTORY_FILE = RESULTS_DIR / "match_history.jsonl"

router = APIRouter(prefix="/api", tags=["data-stream"])

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


def _event(event_type: str, data: Any) -> dict:
    return {"event": event_type, "data": json.dumps(data, default=str)}


def _get_ratings() -> list[dict]:
    from tool_helpers import compute_h2h_avg_winrate
    data = _cached_read("ds_ratings", RATINGS_FILE)
    if not data:
        return []
    bot_stats_data = _cached_read("ds_bot_stats_ratings", BOT_STATS_FILE) or {}
    h2h_data = _cached_read("ds_h2h_ratings", H2H_FILE) or {}

    rows = []
    for name, d in data.items():
        r, rd = d["r"], d["rd"]
        bs = bot_stats_data.get(name, {})
        wr = compute_h2h_avg_winrate(name, h2h_data)
        rows.append({
            "name": name,
            "rating": round(r, 1),
            "rd": round(rd, 1),
            "sigma": round(d.get("sigma", 0.06), 4),
            "conservative_rating": round(r - 2 * rd, 1),
            "confidence": _confidence(rd),
            "last_period": d.get("last_period", ""),
            "win_rate": bs.get("win_rate"),
            "games": bs.get("games", 0),
            "h2h_avg_wr": round(wr, 4) if wr is not None else None,
        })
    rows.sort(key=lambda x: x["h2h_avg_wr"] if x["h2h_avg_wr"] is not None else 0.0, reverse=True)
    for i, row in enumerate(rows):
        row["rank"] = i + 1
    return rows


def _get_daemon_status() -> dict:
    from server.state import app_state
    config = app_state.get_config()
    if RATINGS_FILE.exists():
        mtime = os.path.getmtime(RATINGS_FILE)
        age = time.time() - mtime
        status = "active" if age < 60 else ("recent" if age < 600 else "idle")
        return {"status": status, "last_update_age_seconds": round(age, 0), "daemon_enabled": config["daemon_enabled"]}
    return {"status": "unknown", "last_update_age_seconds": -1, "daemon_enabled": config["daemon_enabled"]}


def _get_bots() -> dict:
    from tool_helpers import compute_h2h_avg_winrate
    ratings = _cached_read("ds_ratings_bots", RATINGS_FILE) or {}
    bot_stats_data = _cached_read("ds_bot_stats_bots", BOT_STATS_FILE) or {}
    h2h_data = _cached_read("ds_h2h_bots", H2H_FILE) or {}

    def _count_lines(path: Path) -> int:
        try:
            return sum(1 for _ in open(path, "r", errors="ignore"))
        except Exception:
            return 0

    def _bot_summary(bot_dir: Path, bot_name: str) -> dict:
        version_match = re.search(r"\d+", bot_name)
        version = int(version_match.group()) if version_match else 0
        py_files = list(bot_dir.glob("*.py"))
        total_lines = sum(_count_lines(f) for f in py_files)
        completed = (bot_dir / ".completed").exists()
        r_data = ratings.get(bot_name)
        rating_info = None
        if r_data:
            r, rd = r_data.get("r", 1500), r_data.get("rd", 350)
            rating_info = {"r": round(r, 1), "rd": round(rd, 1), "conservative": round(r - 2 * rd, 1)}
        bs = bot_stats_data.get(bot_name, {})
        wr = compute_h2h_avg_winrate(bot_name, h2h_data)
        return {
            "name": bot_name, "version": version, "completed": completed,
            "total_lines": total_lines, "files": [f.name for f in py_files], "rating": rating_info,
            "win_rate": bs.get("win_rate"), "games": bs.get("games", 0),
            "h2h_avg_wr": round(wr, 4) if wr is not None else None,
        }

    def _version_key(p: Path) -> int:
        m = re.search(r"\d+", p.name)
        return int(m.group()) if m else 0

    active, graveyard = [], []
    if BOTS_DIR.exists():
        for d in sorted(BOTS_DIR.iterdir(), key=_version_key):
            if d.is_dir() and d.name.startswith("claude_v") and d.name != "claude_v0":
                active.append(_bot_summary(d, d.name))
    graveyard_dir = BOTS_DIR / "graveyard"
    if graveyard_dir.exists():
        for d in sorted(graveyard_dir.iterdir(), key=_version_key):
            if d.is_dir() and d.name.startswith("claude_v"):
                s = _bot_summary(d, d.name)
                s["graveyard"] = True
                graveyard.append(s)
    return {"active": active, "graveyard": graveyard}


def _get_match_stats() -> dict:
    stats = _cached_read("ds_stats", STATS_FILE)
    if not stats:
        return {"total_games": 0, "total_pairs": 0, "total_periods": 0, "most_active_pair": "", "most_active_count": 0}
    pairs = stats.get("pairs", {})
    total_games = stats.get("total_games", sum(pairs.values()))
    most_active = max(pairs.items(), key=lambda x: x[1]) if pairs else ("", 0)
    return {
        "total_games": total_games, "total_pairs": len(pairs),
        "total_periods": stats.get("total_periods", 0),
        "most_active_pair": most_active[0], "most_active_count": most_active[1],
    }


def _get_recent_matches(limit: int = 100) -> list[dict]:
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


def _get_match_matrix() -> dict:
    h2h = _cached_read("ds_h2h_matrix", H2H_FILE)
    if not h2h:
        # Fallback to legacy stats
        stats = _cached_read("ds_stats_matrix", STATS_FILE)
        if not stats:
            return {"bots": [], "matrix": []}
        ratings = _cached_read("ds_ratings_matrix", RATINGS_FILE) or {}
        bot_names = sorted(
            ratings.keys(),
            key=lambda n: int(re.search(r"\d+", n).group()) if re.search(r"\d+", n) else 0,
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

    # Build from H2H data
    all_bots = set()
    for k in h2h:
        parts = k.split(" vs ")
        all_bots.update(parts)
    bot_names = sorted(all_bots, key=lambda n: int(re.search(r"\d+", n).group()) if re.search(r"\d+", n) else 0)
    n = len(bot_names)
    # Matrix stores win rates (bot_i vs bot_j = bot_i's win rate)
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


def _get_h2h() -> dict:
    return _cached_read("ds_h2h", H2H_FILE) or {}


def _get_bot_stats() -> dict:
    return _cached_read("ds_bot_stats", BOT_STATS_FILE) or {}


def _get_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    entries = []
    with open(HISTORY_FILE, "r") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        fcntl.flock(f, fcntl.LOCK_UN)
    return entries


def _downsample(entries: list[dict], max_points: int = 200) -> list[dict]:
    if len(entries) <= max_points:
        return entries
    step = max(1, len(entries) // max_points)
    sampled = entries[::step]
    if entries[-1] is not sampled[-1]:
        sampled.append(entries[-1])
    return sampled


def _get_generations() -> list[dict]:
    if not RESULTS_DIR.exists():
        return []
    versions = []
    dirs = sorted(
        (p for p in RESULTS_DIR.iterdir()
         if p.is_dir() and p.name.startswith("v") and (p / "logs").is_dir()),
        key=lambda p: int(re.search(r"\d+", p.name).group()) if re.search(r"\d+", p.name) else 0,
    )
    for p in dirs:
        files = sorted(f.name for f in (p / "logs").iterdir() if f.is_file())
        versions.append({"version": p.name, "files": files})
    return versions


_log = logging.getLogger("data_stream")


@router.get("/data/stream")
async def data_stream():
    async def generate():
        tick = 0
        try:
            while True:
                if tick % 3 == 0:
                    for evt in [
                        _event("ratings", _get_ratings()),
                        _event("daemon", _get_daemon_status()),
                        _event("bots", _get_bots()),
                        _event("stats", _get_match_stats()),
                    ]:
                        try:
                            yield evt
                        except Exception as e:
                            _log.warning("SSE event error: %s", e)
                if tick % 10 == 0:
                    for evt in [
                        _event("matches", _get_recent_matches(100)),
                        _event("generations", _get_generations()),
                    ]:
                        try:
                            yield evt
                        except Exception as e:
                            _log.warning("SSE event error: %s", e)
                if tick % 15 == 0:
                    for evt in [
                        _event("matrix", _get_match_matrix()),
                        _event("h2h", _get_h2h()),
                        _event("bot_stats", _get_bot_stats()),
                        _event("history", _downsample(_get_history())),
                    ]:
                        try:
                            yield evt
                        except Exception as e:
                            _log.warning("SSE event error: %s", e)
                if tick % 30 == 0 and tick % 3 != 0:
                    yield {"event": "ping", "data": "{}"}
                await asyncio.sleep(1)
                tick += 1
        except asyncio.CancelledError:
            pass
    return EventSourceResponse(generate())
