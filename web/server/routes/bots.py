"""Bot management endpoints — list bots, detail, source code."""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from server.cache import cached_read
from server.routes._helpers import build_bot_summary, _bot_sort_key

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BOTS_DIR = PROJECT_ROOT / "bots"
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
BOT_STATS_FILE = RESULTS_DIR / "bot_stats.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"

router = APIRouter(prefix="/api/bots", tags=["bots"])


def _load_ratings() -> dict:
    try:
        return cached_read("ratings", RATINGS_FILE) or {}
    except Exception as e:
        import logging
        logging.getLogger("bots").warning("Failed to load ratings: %s", e)
        return {}


@router.get("")
async def list_bots(include_graveyard: bool = Query(False)):
    """List all active bots and optionally graveyard bots."""
    ratings = _load_ratings()
    bot_stats_data = cached_read("bot_stats", BOT_STATS_FILE) or {}
    h2h_data = cached_read("h2h", H2H_FILE) or {}
    active = []
    graveyard = []

    if BOTS_DIR.exists():
        for d in sorted(BOTS_DIR.iterdir(), key=lambda p: _bot_sort_key(p.name)):
            if d.is_dir() and d.name.startswith("claude_v") and d.name != "claude_v0":
                if (d / ".completed").exists():
                    active.append(build_bot_summary(d, d.name, ratings, bot_stats_data, h2h_data))

    # Graveyard bots
    if include_graveyard:
        graveyard_dir = BOTS_DIR / "graveyard"
        if graveyard_dir.exists():
            for d in sorted(graveyard_dir.iterdir(), key=lambda p: _bot_sort_key(p.name)):
                if d.is_dir() and d.name.startswith("claude_v"):
                    s = build_bot_summary(d, d.name, ratings, bot_stats_data, h2h_data)
                    s["graveyard"] = True
                    graveyard.append(s)

    return {"active": active, "graveyard": graveyard}


@router.get("/{version}")
async def bot_detail(version: int):
    """Get detailed info about a specific bot version."""
    bot_name = f"claude_v{version}"
    active_dir = BOTS_DIR / bot_name
    graveyard_dir = BOTS_DIR / "graveyard" / bot_name

    # Prefer completed version (graveyard) over incomplete active version
    if active_dir.exists() and (active_dir / ".completed").exists():
        bot_dir = active_dir
    elif graveyard_dir.exists() and (graveyard_dir / ".completed").exists():
        bot_dir = graveyard_dir
    elif active_dir.exists():
        bot_dir = active_dir
    elif graveyard_dir.exists():
        bot_dir = graveyard_dir
    else:
        raise HTTPException(status_code=404, detail=f"Bot v{version} not found")

    ratings = _load_ratings()
    bot_stats_data = cached_read("bot_stats_detail", BOT_STATS_FILE) or {}
    h2h_data = cached_read("h2h_detail", H2H_FILE) or {}
    summary = build_bot_summary(bot_dir, bot_name, ratings, bot_stats_data, h2h_data)

    # Try to get git parent from tag
    try:
        import subprocess
        result = subprocess.run(
            ["git", "tag", "-l", f"bot-v{version}", "--format=%(contents)"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT)
        )
        if result.returncode == 0 and result.stdout:
            for line in result.stdout.splitlines():
                if line.startswith("parent:"):
                    summary["parent"] = line.split("parent:")[1].strip()
                    break
    except Exception:
        pass

    return summary


@router.get("/{version}/code/{filename}", response_class=PlainTextResponse)
async def bot_code(version: int, filename: str):
    """Read a bot source file. filename must end with .py."""
    if not filename.endswith(".py") or "/" in filename or "\\" in filename:
        return PlainTextResponse("Invalid filename", status_code=400)

    bot_name = f"claude_v{version}"
    # Check active and graveyard
    for base in [BOTS_DIR / bot_name, BOTS_DIR / "graveyard" / bot_name]:
        path = base / filename
        if path.is_file():
            return PlainTextResponse(path.read_text(errors="replace"))

    return PlainTextResponse(f"File not found: {filename}", status_code=404)
