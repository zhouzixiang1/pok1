"""Bot management endpoints — list bots, detail, source code."""

import io
import subprocess
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, Response

from server.routes._helpers import build_bot_summary, load_strict_strength_snapshot
from evolution_infra import (
    get_published_active_bots_read_only,
)
from bot_namespace import (
    ROLE_PARENT_SOURCE,
    bot_name,
    bot_tag,
    resolve_national_bot_spec,
    strict_generation_identity,
    version_sort_key,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BOTS_DIR = PROJECT_ROOT / "bots"
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
BOT_STATS_FILE = RESULTS_DIR / "bot_stats.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"
MATCH_HISTORY_FILE = RESULTS_DIR / "match_history.jsonl"

router = APIRouter(prefix="/api/bots", tags=["bots"])


def _strict_snapshot() -> dict:
    snapshot = load_strict_strength_snapshot(RESULTS_DIR)
    return snapshot if snapshot.get("available") is True else {}


def _strict_published_inventory() -> list[str]:
    """Return publication authority independently of daemon-cycle freshness."""

    try:
        from epoch_authority import strict_epoch_projection

        projection = strict_epoch_projection(include_checkpoint=False)
    except Exception:
        return []
    if not projection.get("initialized"):
        return []
    names = projection.get("active_bots")
    if not isinstance(names, list) or len(names) != len(set(names)):
        return []
    return sorted((str(name) for name in names), key=version_sort_key)


def _inventory_strength_snapshot(active_names: list[str]) -> dict:
    """Use rating evidence only when it describes this exact published pool."""

    snapshot = _strict_snapshot()
    if set(snapshot.get("active_bots") or []) != set(active_names):
        return {}
    return snapshot


def _decorate_published(summary: dict) -> dict:
    """Project the already-resolved published pool without lifecycle guessing."""

    identity = strict_generation_identity(summary.get("version"))
    if summary.get("name") != identity["canonical_bot_name"]:
        raise ValueError("published_bot_summary_canonical_name_mismatch")
    summary.update({
        **identity,
        "active": True,
        "tagged": True,
        "reaped": False,
        "protocol_eligible": True,
        "protocol_errors": [],
        "lifecycle_status": "active",
        "status_label": "活跃",
        "status_reasons": [],
    })
    return summary


def build_bot_listing(
    ratings: dict,
    bot_stats_data: dict,
    h2h_data: dict,
    *,
    include_history: bool = True,
    active_names: list[str] | tuple[str, ...] | None = None,
    strength_rows_data: list[dict] | tuple[dict, ...] | None = None,
    strength_evidence_available: bool = True,
) -> dict:
    if active_names is None:
        try:
            active_names = get_published_active_bots_read_only()
        except Exception:
            active_names = []
    active_names_set = set(active_names)
    active_dirs = [
        BOTS_DIR / name
        for name in active_names_set
        if (BOTS_DIR / name).is_dir()
    ]
    strength_rows = {
        str(row.get("name")): row
        for row in (strength_rows_data or [])
        if isinstance(row, dict) and row.get("name") in active_names_set
    }
    active = []
    for path in sorted(active_dirs, key=lambda p: version_sort_key(p.name)):
        summary = build_bot_summary(path, path.name, ratings, bot_stats_data, h2h_data, strength_rows)
        summary["strength_evidence_available"] = bool(strength_evidence_available)
        summary["strength_evidence_status"] = (
            "current_evaluation_cycle"
            if strength_evidence_available
            else "awaiting_first_rating_cycle"
        )
        active.append(_decorate_published(summary))

    result = {"active": active}
    if include_history:
        # The former inventory mixed unfinished/reaped directories into the
        # same UI history surface.  Keep the response shape for clients, but
        # only repeat the exact current published pool.
        result["history"] = list(active)
        result["counts"] = {
            "active": len(active),
            "history": len(active),
            "candidate": 0,
            "protocol_ineligible": 0,
            "reaped": 0,
        }
    return result


@router.get("")
async def list_bots(
    include_history: bool = False,
):
    """List only bots in the current strict published evaluation pool."""
    active_names = _strict_published_inventory()
    snapshot = _inventory_strength_snapshot(active_names)
    return build_bot_listing(
        snapshot.get("ratings") or {},
        snapshot.get("bot_stats") or {},
        snapshot.get("h2h") or {},
        include_history=include_history,
        active_names=active_names,
        strength_rows_data=snapshot.get("selection_rows") or [],
        strength_evidence_available=bool(snapshot),
    )


def _resolve_bot_dir(
    version: int,
    active_names: list[str] | tuple[str, ...] | None = None,
) -> Path:
    """Resolve only a current published strict bot; never a candidate/archive."""
    name = bot_name(version)
    active_names = (
        list(active_names)
        if active_names is not None
        else _strict_published_inventory()
    )
    if name not in set(active_names):
        raise HTTPException(status_code=404, detail=f"Bot v{version} not found")
    active_dir = BOTS_DIR / name
    spec = resolve_national_bot_spec(
        active_dir,
        ROLE_PARENT_SOURCE,
        repo_root=BOTS_DIR.parent,
    )
    if spec.eligible:
        return active_dir
    raise HTTPException(status_code=404, detail=f"Bot v{version} not found")


@router.get("/{version}")
async def bot_detail(version: int):
    """Get detailed info about a specific bot version."""
    name = bot_name(version)
    active_names = _strict_published_inventory()
    snapshot = _inventory_strength_snapshot(active_names)
    bot_dir = _resolve_bot_dir(version, active_names)

    ratings = snapshot.get("ratings") or {}
    bot_stats_data = snapshot.get("bot_stats") or {}
    h2h_data = snapshot.get("h2h") or {}
    strength_rows = {
        str(row.get("name")): row
        for row in (snapshot.get("selection_rows") or [])
        if isinstance(row, dict)
    }
    summary = build_bot_summary(bot_dir, name, ratings, bot_stats_data, h2h_data, strength_rows)
    summary["strength_evidence_available"] = bool(snapshot)
    summary["strength_evidence_status"] = (
        "current_evaluation_cycle"
        if snapshot
        else "awaiting_first_rating_cycle"
    )

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

    return _decorate_published(summary)


@router.get("/{version}/download")
async def bot_download(version: int):
    """Download the complete bot source directory as a zip archive.

    Packs the whole bot directory into an in-memory zip (bots are small, at
    most a few hundred KB). Bytecode caches (``__pycache__`` / ``.pyc``) and
    symlinks are excluded — the former are compile artifacts, the latter could
    resolve outside the bot dir and leak unrelated files.
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
    try:
        base = _resolve_bot_dir(version)
    except HTTPException:
        return PlainTextResponse(f"File not found: {filename}", status_code=404)
    path = base / filename
    if path.is_file() and not path.is_symlink():
        return PlainTextResponse(path.read_text(errors="replace"))

    return PlainTextResponse(f"File not found: {filename}", status_code=404)
