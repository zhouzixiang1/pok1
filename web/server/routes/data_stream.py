"""Global data SSE stream — pushes all dashboard data on scheduled intervals."""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from server.cache import cached_read

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


def _event(event_type: str, data: Any) -> dict:
    return {"event": event_type, "data": json.dumps(data, default=str)}


def _get_ratings() -> list[dict]:
    from server.routes._helpers import build_ranked_ratings
    data = cached_read("ds_ratings", RATINGS_FILE)
    if not data:
        return []
    bot_stats_data = cached_read("ds_bot_stats_ratings", BOT_STATS_FILE) or {}
    h2h_data = cached_read("ds_h2h_ratings", H2H_FILE) or {}
    return build_ranked_ratings(data, bot_stats_data, h2h_data)


def _get_daemon_status() -> dict:
    from server.state import app_state
    config = app_state.get_config()
    try:
        if RATINGS_FILE.exists():
            mtime = os.path.getmtime(RATINGS_FILE)
            age = time.time() - mtime
            status = "active" if age < 60 else ("recent" if age < 600 else "idle")
            return {"status": status, "last_update_age_seconds": round(age, 0), "daemon_enabled": config["daemon_enabled"]}
    except OSError:
        pass
    return {"status": "unknown", "last_update_age_seconds": -1, "daemon_enabled": config["daemon_enabled"]}


def _get_bots() -> dict:
    from server.routes._helpers import _bot_sort_key, build_bot_summary
    ratings = cached_read("ds_ratings_bots", RATINGS_FILE) or {}
    bot_stats_data = cached_read("ds_bot_stats_bots", BOT_STATS_FILE) or {}
    h2h_data = cached_read("ds_h2h_bots", H2H_FILE) or {}

    active, graveyard = [], []
    if BOTS_DIR.exists():
        for d in sorted(BOTS_DIR.iterdir(), key=lambda p: _bot_sort_key(p.name)):
            if d.is_dir() and d.name.startswith("claude_v") and d.name != "claude_v0":
                if (d / ".completed").exists():
                    active.append(build_bot_summary(d, d.name, ratings, bot_stats_data, h2h_data))
    graveyard_dir = BOTS_DIR / "graveyard"
    if graveyard_dir.exists():
        for d in sorted(graveyard_dir.iterdir(), key=lambda p: _bot_sort_key(p.name)):
            if d.is_dir() and d.name.startswith("claude_v"):
                s = build_bot_summary(d, d.name, ratings, bot_stats_data, h2h_data)
                s["graveyard"] = True
                graveyard.append(s)
    return {"active": active, "graveyard": graveyard}


def _get_match_stats() -> dict:
    from server.routes._helpers import build_match_stats
    return build_match_stats(cached_read("ds_stats", STATS_FILE))


def _get_recent_matches(limit: int = 100) -> list[dict]:
    from server.routes._helpers import read_jsonl
    return read_jsonl(MATCH_HISTORY_FILE, limit=limit)


def _get_match_matrix() -> dict:
    from server.routes._helpers import build_match_matrix
    h2h = cached_read("ds_h2h_matrix", H2H_FILE)
    stats = cached_read("ds_stats_matrix", STATS_FILE)
    ratings = cached_read("ds_ratings_matrix", RATINGS_FILE) or {}
    return build_match_matrix(h2h, ratings, stats)


def _get_h2h() -> dict:
    return cached_read("ds_h2h", H2H_FILE) or {}


def _get_bot_stats() -> dict:
    return cached_read("ds_bot_stats", BOT_STATS_FILE) or {}


def _get_history() -> list[dict]:
    from server.routes._helpers import read_jsonl
    return read_jsonl(HISTORY_FILE, reverse=False)


def _downsample(entries: list[dict], max_points: int = 200) -> list[dict]:
    from server.routes._helpers import downsample
    return downsample(entries, max_points)


def _get_generations() -> list[dict]:
    from server.routes._helpers import list_generation_dirs
    return list_generation_dirs(RESULTS_DIR)


_log = logging.getLogger("data_stream")


@router.get("/data/stream")
async def data_stream(request: Request):
    async def generate():
        tick = 0
        try:
            while True:
                if await request.is_disconnected():
                    break
                if tick % 3 == 0:
                    try:
                        events = [
                            _event("ratings", _get_ratings()),
                            _event("daemon", _get_daemon_status()),
                            _event("bots", _get_bots()),
                            _event("stats", _get_match_stats()),
                        ]
                        # Scheduler queue status (push alongside daemon every 3s)
                        try:
                            from battle_scheduler import BATTLE_JOBS_FILE, BATTLE_CLAIMED_FILE, BATTLE_RESULTS_FILE, _read_jsonl
                            _sj = _read_jsonl(BATTLE_JOBS_FILE)
                            _sc = _read_jsonl(BATTLE_CLAIMED_FILE)
                            _sr = _read_jsonl(BATTLE_RESULTS_FILE)
                            events.append(_event("scheduler", {
                                "pending_jobs": len(_sj),
                                "claimed_jobs": len(_sc),
                                "recent_results": len(_sr),
                            }))
                        except Exception:
                            pass
                        # 429 rate-limit status (push alongside daemon every 3s)
                        try:
                            from rate_limiter import rate_limiter
                            if rate_limiter.is_blocked():
                                events.append(_event("rate_limit", {
                                    "blocked": True,
                                    "reset_time": rate_limiter.reset_time_str(),
                                    "wait_seconds": round(rate_limiter.wait_seconds(), 0),
                                }))
                            else:
                                events.append(_event("rate_limit", {"blocked": False}))
                        except Exception:
                            pass
                    except Exception as e:
                        _log.warning("SSE data fetch error (3s): %s", e)
                        events = []
                    for evt in events:
                        try:
                            yield evt
                        except Exception as e:
                            _log.warning("SSE event error: %s", e)
                if tick % 10 == 0:
                    try:
                        events = [
                            _event("matches", _get_recent_matches(100)),
                            _event("generations", _get_generations()),
                        ]
                    except Exception as e:
                        _log.warning("SSE data fetch error (10s): %s", e)
                        events = []
                    for evt in events:
                        try:
                            yield evt
                        except Exception as e:
                            _log.warning("SSE event error: %s", e)
                if tick % 15 == 0:
                    try:
                        events = [
                            _event("matrix", _get_match_matrix()),
                            _event("h2h", _get_h2h()),
                            _event("bot_stats", _get_bot_stats()),
                            _event("history", _downsample(_get_history())),
                        ]
                    except Exception as e:
                        _log.warning("SSE data fetch error (15s): %s", e)
                        events = []
                    for evt in events:
                        try:
                            yield evt
                        except Exception as e:
                            _log.warning("SSE event error: %s", e)
                if tick % 30 == 0:
                    yield {"event": "ping", "data": "{}"}
                await asyncio.sleep(1)
                tick += 1
        except asyncio.CancelledError:
            pass
    return EventSourceResponse(generate())
