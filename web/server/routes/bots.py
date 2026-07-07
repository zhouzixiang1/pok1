"""Bot management endpoints — list bots, detail, source code."""

import io
import re
import subprocess
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse, Response

from server.cache import cached_read
from server.routes._helpers import build_bot_summary
from rating_snapshot import build_strength_rows
from evolution_infra import active_bot_protocol_errors, get_active_bots
from bot_namespace import ACTIVE_BOT_PREFIX, bot_name, bot_tag, parse_bot_version, parse_tag_version, version_sort_key

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BOTS_DIR = PROJECT_ROOT / "bots"
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
BOT_STATS_FILE = RESULTS_DIR / "bot_stats.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"
MATCH_HISTORY_FILE = RESULTS_DIR / "match_history.jsonl"

router = APIRouter(prefix="/api/bots", tags=["bots"])


def _load_ratings() -> dict:
    try:
        return cached_read("ratings", RATINGS_FILE) or {}
    except Exception as e:
        import logging
        logging.getLogger("bots").warning("Failed to load ratings: %s", e)
        return {}


def _tagged_versions() -> set[int]:
    try:
        result = subprocess.run(
            ["git", "tag", "-l", "national-bot-v*", "--sort=version:refname"],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=10,
        )
    except Exception:
        return set()
    if result.returncode != 0:
        return set()
    versions = set()
    for line in result.stdout.splitlines():
        version = parse_tag_version(line.strip())
        if version is not None:
            versions.add(version)
    return versions


def _reaped_versions() -> set[int]:
    path = RESULTS_DIR / "reaped_bots.jsonl"
    versions = set()
    if not path.exists():
        return versions
    try:
        for line in path.read_text(errors="replace").splitlines():
            match = re.search(r'"version"\s*:\s*(\d+)', line)
            if match:
                versions.add(int(match.group(1)))
    except OSError:
        return versions
    return versions


def _bot_dirs(include_graveyard: bool) -> list[tuple[Path, bool]]:
    dirs: list[tuple[Path, bool]] = []
    if BOTS_DIR.exists():
        for path in BOTS_DIR.iterdir():
            if path.is_dir() and path.name.startswith(ACTIVE_BOT_PREFIX) and parse_bot_version(path.name):
                dirs.append((path, False))
    if include_graveyard:
        graveyard_dir = BOTS_DIR / "graveyard"
        if graveyard_dir.exists():
            for path in graveyard_dir.iterdir():
                if path.is_dir() and path.name.startswith(ACTIVE_BOT_PREFIX) and parse_bot_version(path.name):
                    dirs.append((path, True))
    return sorted(dirs, key=lambda item: version_sort_key(item[0].name))


def _status_label(status: str) -> str:
    return {
        "active": "活跃",
        "candidate": "候选",
        "reaped": "已淘汰",
        "protocol_ineligible": "协议不合规",
        "untagged": "未打标签",
        "incomplete": "未完成",
        "graveyard": "已归档",
        "inactive": "历史",
    }.get(status, status)


def _decorate_lifecycle(
    summary: dict,
    *,
    active_names: set[str],
    tagged_versions: set[int],
    reaped_versions: set[int],
    is_graveyard: bool,
) -> dict:
    version = summary["version"]
    name = summary["name"]
    completed = bool(summary.get("completed"))
    tagged = version in tagged_versions
    reaped = version in reaped_versions
    protocol_errors = [] if is_graveyard else active_bot_protocol_errors(version)
    reasons: list[str] = []

    if is_graveyard:
        status = "graveyard"
        reasons.append("bot source is under bots/graveyard/")
    elif name in active_names:
        status = "active"
    elif not completed and not tagged:
        status = "candidate"
        reasons.append("missing .completed sentinel and national-bot tag")
    elif reaped:
        status = "reaped"
        reasons.append("removed from active pool by reap_weakest")
    elif protocol_errors:
        status = "protocol_ineligible"
        reasons.append("fails current national native protocol contract")
    elif not tagged:
        status = "untagged"
        reasons.append("missing national-bot-v<N> tag")
    elif not completed:
        status = "incomplete"
        reasons.append("missing .completed sentinel")
    else:
        status = "inactive"
        reasons.append("not selected into current active pool")

    summary.update({
        "active": status == "active",
        "tagged": tagged,
        "reaped": reaped,
        "protocol_eligible": not protocol_errors,
        "protocol_errors": protocol_errors,
        "lifecycle_status": status,
        "status_label": _status_label(status),
        "status_reasons": reasons,
        "graveyard": is_graveyard,
    })
    return summary


def build_bot_listing(
    ratings: dict,
    bot_stats_data: dict,
    h2h_data: dict,
    *,
    include_graveyard: bool = False,
    include_history: bool = True,
) -> dict:
    active_names = set(get_active_bots())
    active_dirs = [BOTS_DIR / name for name in active_names if (BOTS_DIR / name).is_dir()]
    strength_rows = {
        row["name"]: row
        for row in build_strength_rows(
            ratings,
            bot_stats_data,
            h2h_data,
            active_bots=sorted(active_names, key=version_sort_key),
            match_history_path=MATCH_HISTORY_FILE,
        )
    }
    tagged_versions = _tagged_versions()
    reaped_versions = _reaped_versions()

    active = []
    for path in sorted(active_dirs, key=lambda p: version_sort_key(p.name)):
        summary = build_bot_summary(path, path.name, ratings, bot_stats_data, h2h_data, strength_rows)
        active.append(_decorate_lifecycle(
            summary,
            active_names=active_names,
            tagged_versions=tagged_versions,
            reaped_versions=reaped_versions,
            is_graveyard=False,
        ))

    graveyard = []
    history = []
    for path, is_graveyard in _bot_dirs(include_graveyard):
        summary = build_bot_summary(path, path.name, ratings, bot_stats_data, h2h_data, strength_rows)
        summary = _decorate_lifecycle(
            summary,
            active_names=active_names,
            tagged_versions=tagged_versions,
            reaped_versions=reaped_versions,
            is_graveyard=is_graveyard,
        )
        if is_graveyard:
            graveyard.append(summary)
        elif include_history:
            history.append(summary)

    result = {"active": active, "graveyard": graveyard}
    if include_history:
        result["history"] = history
        result["counts"] = {
            "active": len(active),
            "history": len(history),
            "graveyard": len(graveyard),
            "candidate": sum(1 for bot in history if bot["lifecycle_status"] == "candidate"),
            "protocol_ineligible": sum(1 for bot in history if bot["lifecycle_status"] == "protocol_ineligible"),
            "reaped": sum(1 for bot in history if bot["lifecycle_status"] == "reaped"),
        }
    return result


@router.get("")
async def list_bots(
    include_graveyard: bool = Query(False),
    include_history: bool = Query(False),
):
    """List active bots and optionally historical/inactive bot inventory."""
    ratings = _load_ratings()
    bot_stats_data = cached_read("bot_stats", BOT_STATS_FILE) or {}
    h2h_data = cached_read("h2h", H2H_FILE) or {}
    return build_bot_listing(
        ratings,
        bot_stats_data,
        h2h_data,
        include_graveyard=include_graveyard,
        include_history=include_history,
    )


def _resolve_bot_dir(version: int) -> Path:
    """Resolve the bot source directory for a version.

    Prefers a ``.completed`` copy (the committed truth) and prefers the active
    pool over the graveyard when both exist. Raises 404 if unknown.
    """
    name = bot_name(version)
    active_dir = BOTS_DIR / name
    graveyard_dir = BOTS_DIR / "graveyard" / name
    if active_dir.exists() and (active_dir / ".completed").exists():
        return active_dir
    if graveyard_dir.exists() and (graveyard_dir / ".completed").exists():
        return graveyard_dir
    if active_dir.exists():
        return active_dir
    if graveyard_dir.exists():
        return graveyard_dir
    raise HTTPException(status_code=404, detail=f"Bot v{version} not found")


@router.get("/{version}")
async def bot_detail(version: int):
    """Get detailed info about a specific bot version."""
    name = bot_name(version)
    bot_dir = _resolve_bot_dir(version)

    ratings = _load_ratings()
    bot_stats_data = cached_read("bot_stats_detail", BOT_STATS_FILE) or {}
    h2h_data = cached_read("h2h_detail", H2H_FILE) or {}
    strength_rows = {
        row["name"]: row
        for row in build_strength_rows(
            ratings,
            bot_stats_data,
            h2h_data,
            active_bots=list(ratings.keys()),
            match_history_path=MATCH_HISTORY_FILE,
        )
    }
    summary = build_bot_summary(bot_dir, name, ratings, bot_stats_data, h2h_data, strength_rows)

    # Try to get git parent from tag
    try:
        import subprocess
        result = subprocess.run(
            ["git", "tag", "-l", bot_tag(version), "--format=%(contents)"],
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


@router.get("/{version}/download")
async def bot_download(version: int):
    """Download the complete bot source directory as a zip archive.

    Packs the whole bot directory into an in-memory zip (bots are small, at
    most a few hundred KB). Bytecode caches (``__pycache__`` / ``.pyc``) and
    symlinks are excluded — the former are compile artifacts, the latter could
    resolve outside the bot dir and leak unrelated files. Works for both active
    and graveyard bots.
    """
    bot_dir = _resolve_bot_dir(version)
    bot_name = bot_dir.name

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(bot_dir.rglob("*")):
            # Skip symlinks first: is_file() follows them, and a symlink may
            # point outside the bot dir (information leak). Real source only.
            if path.is_symlink():
                continue
            if not path.is_file():
                continue
            rel = path.relative_to(bot_dir)
            # Defense-in-depth: never emit arcnames that escape the archive root.
            if rel.is_absolute() or ".." in rel.parts:
                continue
            if "__pycache__" in rel.parts or rel.suffix == ".pyc":
                continue
            try:
                zf.write(path, arcname=str(rel))
            except (FileNotFoundError, PermissionError):
                # Vanished/locked mid-zip (e.g. __pycache__ rotation under the
                # daemon) — skip silently rather than 500.
                continue

    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{bot_name}.zip"'},
    )


@router.get("/{version}/code/{filename}", response_class=PlainTextResponse)
async def bot_code(version: int, filename: str):
    """Read a bot source file. filename must end with .py."""
    if not filename.endswith(".py") or "/" in filename or "\\" in filename:
        return PlainTextResponse("Invalid filename", status_code=400)

    name = bot_name(version)
    # Check active and graveyard
    for base in [BOTS_DIR / name, BOTS_DIR / "graveyard" / name]:
        path = base / filename
        if path.is_file():
            return PlainTextResponse(path.read_text(errors="replace"))

    return PlainTextResponse(f"File not found: {filename}", status_code=404)
