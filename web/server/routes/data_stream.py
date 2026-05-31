"""Global data SSE stream — pushes all dashboard data on scheduled intervals."""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from server.cache import cached_read
from server.routes._helpers import (
    build_bot_summary, build_match_matrix, build_match_stats,
    build_ranked_ratings, downsample, read_jsonl,
)

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
    data = cached_read("ds_ratings", RATINGS_FILE)
    bot_stats_data = cached_read("ds_bot_stats_ratings", BOT_STATS_FILE) or {}
    h2h_data = cached_read("ds_h2h_ratings", H2H_FILE) or {}
    return build_ranked_ratings(data or {}, bot_stats_data, h2h_data)


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
    ratings = cached_read("ds_ratings_bots", RATINGS_FILE) or {}
    bot_stats_data = cached_read("ds_bot_stats_bots", BOT_STATS_FILE) or {}
    h2h_data = cached_read("ds_h2h_bots", H2H_FILE) or {}

    def _version_key(p: Path) -> int:
        m = re.search(r"\d+", p.name)
        return int(m.group()) if m else 0

    active, graveyard = [], []
    if BOTS_DIR.exists():
        for d in sorted(BOTS_DIR.iterdir(), key=_version_key):
            if d.is_dir() and d.name.startswith("claude_v") and d.name != "claude_v0":
                if (d / ".completed").exists():
                    active.append(build_bot_summary(d, d.name, ratings, bot_stats_data, h2h_data))
    graveyard_dir = BOTS_DIR / "graveyard"
    if graveyard_dir.exists():
        for d in sorted(graveyard_dir.iterdir(), key=_version_key):
            if d.is_dir() and d.name.startswith("claude_v"):
                s = build_bot_summary(d, d.name, ratings, bot_stats_data, h2h_data)
                s["graveyard"] = True
                graveyard.append(s)
    return {"active": active, "graveyard": graveyard}


def _get_match_stats() -> dict:
    stats = cached_read("ds_stats", STATS_FILE)
    return build_match_stats(stats)


def _get_match_matrix() -> dict:
    h2h = cached_read("ds_h2h_matrix", H2H_FILE)
    ratings = cached_read("ds_ratings_matrix", RATINGS_FILE)
    stats = cached_read("ds_stats_matrix", STATS_FILE)
    return build_match_matrix(h2h, ratings, stats)


def _get_h2h() -> dict:
    return cached_read("ds_h2h", H2H_FILE) or {}


def _get_bot_stats() -> dict:
    return cached_read("ds_bot_stats", BOT_STATS_FILE) or {}


def _get_history() -> list[dict]:
    return read_jsonl(HISTORY_FILE, reverse=False)


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
                        _event("matches", read_jsonl(MATCH_HISTORY_FILE, limit=100)),
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
                        _event("history", downsample(_get_history())),
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
