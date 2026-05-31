"""Bot management endpoints — list bots, detail, source code."""

import fcntl
import json
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BOTS_DIR = PROJECT_ROOT / "bots"
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"

router = APIRouter(prefix="/api/bots", tags=["bots"])

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 3.0


def _cached_read(key: str, path: Path) -> Any:
    now = time.time()
    if key in _cache:
        mtime, data = _cache[key]
        if now - mtime < _CACHE_TTL:
            return data
    if not path.exists():
        return None
    with open(path, "r") as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        data = json.load(f)
        fcntl.flock(f, fcntl.LOCK_UN)
    _cache[key] = (now, data)
    return data


def _load_ratings() -> dict:
    now = time.time()
    if "ratings" in _cache:
        mtime, data = _cache["ratings"]
        if now - mtime < _CACHE_TTL:
            return data
    if not RATINGS_FILE.exists():
        return {}
    try:
        with open(RATINGS_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
        _cache["ratings"] = (now, data)
        return data
    except Exception as e:
        import logging
        logging.getLogger("bots").warning("Failed to load ratings: %s", e)
        return {}


def _count_lines(path: Path) -> int:
    try:
        return sum(1 for _ in open(path, "r", errors="ignore"))
    except Exception:
        return 0


BOT_STATS_FILE = RESULTS_DIR / "bot_stats.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"


def _bot_summary(bot_dir: Path, bot_name: str, ratings: dict, bot_stats_data: dict, h2h_data: dict) -> dict:
    from tool_helpers import compute_h2h_avg_winrate
    version_match = re.search(r"\d+", bot_name)
    version = int(version_match.group()) if version_match else 0

    py_files = list(bot_dir.glob("*.py"))
    total_lines = sum(_count_lines(f) for f in py_files)
    completed = (bot_dir / ".completed").exists()

    r_data = ratings.get(bot_name)
    rating_info = None
    if r_data:
        r, rd = r_data.get("r", 1500), r_data.get("rd", 350)
        rating_info = {
            "r": round(r, 1),
            "rd": round(rd, 1),
            "conservative": round(r - 2 * rd, 1),
        }

    bs = bot_stats_data.get(bot_name, {})
    wr = compute_h2h_avg_winrate(bot_name, h2h_data)

    return {
        "name": bot_name,
        "version": version,
        "completed": completed,
        "total_lines": total_lines,
        "files": [f.name for f in py_files],
        "rating": rating_info,
        "win_rate": bs.get("win_rate"),
        "games": bs.get("games", 0),
        "h2h_avg_wr": round(wr, 4) if wr is not None else None,
    }


@router.get("")
async def list_bots(include_graveyard: bool = Query(False)):
    """List all active bots and optionally graveyard bots."""
    ratings = _load_ratings()
    bot_stats_data = _cached_read("bot_stats", BOT_STATS_FILE) or {}
    h2h_data = _cached_read("h2h", H2H_FILE) or {}
    active = []
    graveyard = []

    # Active bots — only include directories with .completed
    def _version_key(p: Path) -> int:
        m = re.search(r'\d+', p.name)
        return int(m.group()) if m else 0

    if BOTS_DIR.exists():
        for d in sorted(BOTS_DIR.iterdir(), key=_version_key):
            if d.is_dir() and d.name.startswith("claude_v") and d.name != "claude_v0":
                if (d / ".completed").exists():
                    active.append(_bot_summary(d, d.name, ratings, bot_stats_data, h2h_data))

    # Graveyard bots
    if include_graveyard:
        graveyard_dir = BOTS_DIR / "graveyard"
        if graveyard_dir.exists():
            for d in sorted(graveyard_dir.iterdir(), key=_version_key):
                if d.is_dir() and d.name.startswith("claude_v"):
                    s = _bot_summary(d, d.name, ratings, bot_stats_data, h2h_data)
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
    bot_stats_data = _cached_read("bot_stats_detail", BOT_STATS_FILE) or {}
    h2h_data = _cached_read("h2h_detail", H2H_FILE) or {}
    summary = _bot_summary(bot_dir, bot_name, ratings, bot_stats_data, h2h_data)

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
