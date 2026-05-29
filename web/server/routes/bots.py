"""Bot management endpoints — list bots, detail, source code."""

import fcntl
import json
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BOTS_DIR = PROJECT_ROOT / "bots"
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"

router = APIRouter(prefix="/api/bots", tags=["bots"])

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 3.0


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
    except Exception:
        return {}


def _count_lines(path: Path) -> int:
    try:
        return sum(1 for _ in open(path, "r", errors="ignore"))
    except Exception:
        return 0


def _bot_summary(bot_dir: Path, bot_name: str, ratings: dict) -> dict:
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

    return {
        "name": bot_name,
        "version": version,
        "completed": completed,
        "total_lines": total_lines,
        "files": [f.name for f in py_files],
        "rating": rating_info,
    }


@router.get("")
async def list_bots(include_graveyard: bool = Query(False)):
    """List all active bots and optionally graveyard bots."""
    ratings = _load_ratings()
    active = []
    graveyard = []

    # Active bots
    if BOTS_DIR.exists():
        for d in sorted(BOTS_DIR.iterdir()):
            if d.is_dir() and d.name.startswith("claude_v") and d.name != "claude_v0":
                active.append(_bot_summary(d, d.name, ratings))

    # Graveyard bots
    if include_graveyard:
        graveyard_dir = BOTS_DIR / "graveyard"
        if graveyard_dir.exists():
            for d in sorted(graveyard_dir.iterdir()):
                if d.is_dir() and d.name.startswith("claude_v"):
                    s = _bot_summary(d, d.name, ratings)
                    s["graveyard"] = True
                    graveyard.append(s)

    return {"active": active, "graveyard": graveyard}


@router.get("/{version}")
async def bot_detail(version: int):
    """Get detailed info about a specific bot version."""
    bot_name = f"claude_v{version}"
    bot_dir = BOTS_DIR / bot_name

    # Check graveyard too
    if not bot_dir.exists():
        bot_dir = BOTS_DIR / "graveyard" / bot_name
        if not bot_dir.exists():
            return {"error": f"Bot v{version} not found"}

    ratings = _load_ratings()
    summary = _bot_summary(bot_dir, bot_name, ratings)

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
