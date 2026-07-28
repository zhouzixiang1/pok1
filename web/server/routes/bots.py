"""Bot management endpoints — list bots, detail, source code."""

import io
import subprocess
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, Response

from blocking_runtime import run_blocking_isolated
from server.routes._helpers import build_bot_summary, load_strict_strength_snapshot
from bot_namespace import (
    ROLE_PARENT_SOURCE,
    bot_name,
    bot_tag,
    resolve_national_bot_spec,
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


def _strict_published_authority(
    projection: dict | None = None,
) -> tuple[list[str], dict[str, dict]]:
    """Return one cross-bound published inventory/ordinal projection."""

    if projection is None:
        try:
            from epoch_authority import strict_epoch_projection

            projection = strict_epoch_projection(include_checkpoint=False)
        except Exception:
            return [], {}
    if not projection.get("initialized"):
        return [], {}
    names = projection.get("active_bots")
    if not isinstance(names, list) or len(names) != len(set(names)):
        return [], {}
    names = sorted((str(name) for name in names), key=version_sort_key)
    identities = projection.get("strict_published_bot_identities")
    if not isinstance(identities, list) or len(identities) < len(names):
        return [], {}
    by_name: dict[str, dict] = {}
    required_identity_keys = {
        "generation_ordinal",
        "canonical_version",
        "canonical_bot_name",
        "canonical_tag",
    }
    for raw_identity in identities:
        if not isinstance(raw_identity, dict):
            return [], {}
        identity = dict(raw_identity)
        if set(identity) != required_identity_keys:
            return [], {}
        version = identity.get("canonical_version")
        ordinal = identity.get("generation_ordinal")
        name = identity.get("canonical_bot_name")
        tag = identity.get("canonical_tag")
        if (
            type(version) is not int
            or type(ordinal) is not int
            or ordinal <= 0
            or not isinstance(name, str)
            or name != bot_name(version)
            or tag != bot_tag(version)
            or name in by_name
        ):
            return [], {}
        by_name[name] = identity
    if not set(names).issubset(set(by_name)):
        return [], {}
    ordered_history = sorted(by_name, key=version_sort_key)
    for expected_ordinal, name in enumerate(ordered_history, start=1):
        if by_name[name]["generation_ordinal"] != expected_ordinal:
            return [], {}
    return names, by_name


def _strict_published_inventory() -> list[str]:
    """Compatibility projection of the paired publication authority names."""

    return _strict_published_authority()[0]


def _inventory_strength_snapshot(active_names: list[str]) -> dict:
    """Use rating evidence only when it describes this exact published pool."""

    snapshot = _strict_snapshot()
    if set(snapshot.get("active_bots") or []) != set(active_names):
        return {}
    return snapshot


def _decorate_published(summary: dict, identity: dict) -> dict:
    """Project the already-resolved published pool without lifecycle guessing."""

    if not isinstance(identity, dict):
        raise ValueError("published_bot_summary_identity_missing")
    if summary.get("name") != identity["canonical_bot_name"]:
        raise ValueError("published_bot_summary_canonical_name_mismatch")
    if summary.get("version") != identity.get("canonical_version"):
        raise ValueError("published_bot_summary_canonical_version_mismatch")
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
    generation_identities: dict[str, dict] | None = None,
    strength_rows_data: list[dict] | tuple[dict, ...] | None = None,
    strength_evidence_available: bool = True,
) -> dict:
    if active_names is None:
        active_names, generation_identities = _strict_published_authority()
    if generation_identities is None:
        # Callers may supply a pool only for an isolated test/fixture, but they
        # must still supply the backend-owned ordinal identity. Never recreate
        # it from version arithmetic or the filtered pool.
        generation_identities = {}
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
        identity = generation_identities.get(path.name)
        if not isinstance(identity, dict):
            continue
        active.append(_decorate_published(summary, identity))

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


def _list_bots_blocking(include_history: bool) -> dict:
    """Synchronous read of the strict published evaluation pool.

    Runs on an isolated worker thread (see ``list_bots``) so the blocking
    git/file reads it transitively performs never freeze the uvicorn event
    loop. Mirrors the ``_run_control_observer_http_snapshot`` pattern in
    ``control.py``.
    """
    active_names, generation_identities = _strict_published_authority()
    snapshot = _inventory_strength_snapshot(active_names)
    return build_bot_listing(
        snapshot.get("ratings") or {},
        snapshot.get("bot_stats") or {},
        snapshot.get("h2h") or {},
        include_history=include_history,
        active_names=active_names,
        generation_identities=generation_identities,
        strength_rows_data=snapshot.get("selection_rows") or [],
        strength_evidence_available=bool(snapshot),
    )


@router.get("")
async def list_bots(
    include_history: bool = False,
):
    """List only bots in the current strict published evaluation pool.

    Offloaded to an isolated worker thread: the pool read transitively
    performs blocking git/file operations (including ``git ls-remote origin``
    when ``POK_REQUIRE_EVOLUTION_PUSH=1``) which must not stall the shared
    uvicorn event loop and starve every other HTTP handler (notably
    ``/api/control/health``).
    """
    return await run_blocking_isolated(
        _list_bots_blocking,
        include_history,
        thread_name_prefix="list-bots",
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


def _bot_detail_blocking(version: int) -> dict:
    """Synchronous bot-detail read for offloaded execution.

    Runs on an isolated worker thread (see ``bot_detail``) so the blocking
    git/file reads it transitively performs (including ``git ls-remote origin``
    via ``_strict_published_authority`` and the ``git tag`` parent lookup) never
    freeze the shared uvicorn event loop and starve every other HTTP handler.
    Mirrors the ``_list_bots_blocking`` pattern.
    """
    name = bot_name(version)
    active_names, generation_identities = _strict_published_authority()
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

    # Try to get git parent from tag. This is a synchronous subprocess call —
    # safe here because the whole body runs off the event loop.
    try:
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

    identity = generation_identities.get(name)
    if not isinstance(identity, dict):
        raise HTTPException(status_code=404, detail=f"Bot v{version} not found")
    return _decorate_published(summary, identity)


@router.get("/{version}")
async def bot_detail(version: int):
    """Get detailed info about a specific bot version.

    Offloaded to an isolated worker thread: the detail read transitively
    performs blocking git/file operations (published-authority resolution,
    strength snapshot, and the ``git tag`` parent lookup) which must not stall
    the shared uvicorn event loop (see the ``list_bots`` offload rationale and
    the prior ``/api/bots`` stall fix).
    """
    return await run_blocking_isolated(
        _bot_detail_blocking,
        version,
        thread_name_prefix="bot-detail",
    )


def _bot_download_blocking(version: int) -> tuple[bytes, str]:
    """Synchronous bot-source zip packing for offloaded execution.

    Runs on an isolated worker thread (see ``bot_download``) so the directory
    walk + in-memory zip of the bot dir never freezes the event loop.
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

    return buf.getvalue(), bot_name


@router.get("/{version}/download")
async def bot_download(version: int):
    """Download the complete bot source directory as a zip archive.

    Packs the whole bot directory into an in-memory zip (bots are small, at
    most a few hundred KB). Bytecode caches (``__pycache__`` / ``.pyc``) and
    symlinks are excluded — the former are compile artifacts, the latter could
    resolve outside the bot dir and leak unrelated files.

    Offloaded to an isolated worker thread so the directory walk and zip
    packing never stall the event loop.
    """
    data, bot_dir_name = await run_blocking_isolated(
        _bot_download_blocking,
        version,
        thread_name_prefix="bot-download",
    )
    return Response(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{bot_dir_name}.zip"'},
    )


def _bot_code_blocking(version: int, filename: str) -> str | None:
    """Synchronous bot source-file read for offloaded execution.

    Returns the file text, or ``None`` when the file is absent/invalid. Runs on
    an isolated worker thread (see ``bot_code``) so the published-authority
    resolution and file read never freeze the event loop.
    """
    if not filename.endswith(".py") or "/" in filename or "\\" in filename:
        return None
    try:
        base = _resolve_bot_dir(version)
    except HTTPException:
        return None
    path = base / filename
    if path.is_file() and not path.is_symlink():
        return path.read_text(errors="replace")
    return None


@router.get("/{version}/code/{filename}", response_class=PlainTextResponse)
async def bot_code(version: int, filename: str):
    """Read a bot source file. filename must end with .py.

    Offloaded to an isolated worker thread so the published-authority
    resolution (which can reach ``git ls-remote origin``) and the file read
    never stall the event loop.
    """
    text = await run_blocking_isolated(
        _bot_code_blocking,
        version,
        filename,
        thread_name_prefix="bot-code",
    )
    if text is None:
        if not filename.endswith(".py") or "/" in filename or "\\" in filename:
            return PlainTextResponse("Invalid filename", status_code=400)
        return PlainTextResponse(f"File not found: {filename}", status_code=404)
    return PlainTextResponse(text)
