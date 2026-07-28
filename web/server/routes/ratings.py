"""Rating endpoints — Glicko-2 ratings and history."""

import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from blocking_runtime import run_blocking_isolated
from server.cache import cached_by_mtime
from server.routes._helpers import (
    load_strict_strength_snapshot,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
STATS_FILE = RESULTS_DIR / "elo_daemon_stats.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"
BOT_STATS_FILE = RESULTS_DIR / "bot_stats.json"
HISTORY_FILE = RESULTS_DIR / "rating_history.jsonl"
MATCH_HISTORY_FILE = RESULTS_DIR / "match_history.jsonl"
# The daemon republishes this manifest atomically when a new evaluation cycle's
# ratings/h2h/history land; its mtime is the single invalidation signal for the
# whole strength snapshot family (ratings, history, h2h, bot-stats).
CYCLE_MANIFEST_FILE = RESULTS_DIR / "evaluation_cycle_manifest.json"

router = APIRouter(prefix="/api", tags=["ratings"])


def _snapshot() -> dict:
    # mtime-keyed cache: the snapshot reopens multiple JSON/JSONL files on every
    # call and the frontend polls several ratings endpoints in one burst. The
    # manifest mtime changes the instant a new cycle is published, so the cache
    # auto-invalidates exactly when the data does; the 2s TTL only bounds
    # staleness during a long quiet period. Tests monkeypatch this function, so
    # the cache never affects test isolation.
    return cached_by_mtime(
        "ratings:strict_strength_snapshot",
        CYCLE_MANIFEST_FILE,
        lambda: load_strict_strength_snapshot(RESULTS_DIR),
    )


def strict_daemon_status(
    snapshot: dict | None = None,
    *,
    epoch: dict | None = None,
) -> dict:
    """Project daemon observability from the same strict epoch authority.

    ``daemon_enabled`` means effective availability in the current epoch. The
    persisted preference is exposed separately so an old config value cannot
    make a stopped/pre-reset daemon look active.
    """

    from server.state import app_state

    configured = bool(app_state.get_config()["daemon_enabled"])
    if epoch is None:
        try:
            from epoch_authority import strict_epoch_projection

            epoch = strict_epoch_projection(include_checkpoint=False)
        except Exception:
            epoch = {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "epoch_authority_unavailable",
                "initialized": False,
            }
    if not epoch.get("initialized"):
        return {
            "status": "blocked",
            "reason": "policy_epoch_not_initialized",
            "epoch_state": epoch.get("state", "epoch_authority_unavailable"),
            "last_update_age_seconds": -1,
            "daemon_enabled": False,
            "daemon_configured": configured,
        }

    try:
        from server.routes.control import _daemon_health_snapshot

        health = _daemon_health_snapshot()
    except Exception:
        health = {
            "alive": False,
            "heartbeat_stale": False,
        }
    process_alive = bool(health.get("alive"))
    heartbeat_stale = bool(health.get("heartbeat_stale"))
    effective_alive = process_alive and not heartbeat_stale
    activity_state = str(health.get("activity_state") or "")
    if effective_alive and activity_state.startswith("waiting_for_"):
        status = "idle"
        reason = activity_state
    elif effective_alive:
        status = "active"
        reason = None
    elif process_alive:
        status = "degraded"
        reason = "daemon_heartbeat_stale"
    elif configured:
        status = "stopped"
        reason = "daemon_process_not_alive"
    else:
        status = "disabled"
        reason = "daemon_not_configured"

    snapshot = _snapshot() if snapshot is None else snapshot
    cycle_manifest = RESULTS_DIR / "evaluation_cycle_manifest.json"
    evidence_age = -1
    if snapshot and cycle_manifest.exists():
        try:
            evidence_age = round(time.time() - cycle_manifest.stat().st_mtime, 0)
        except OSError:
            evidence_age = -1
    evidence_available = snapshot.get("available") is True
    active_count = len(epoch.get("active_bots") or [])
    if active_count == 0:
        evidence_status = "active_pool_empty"
    elif active_count == 1:
        evidence_status = "active_pool_singleton"
    elif evidence_available:
        evidence_status = "current_evaluation_cycle"
    else:
        evidence_status = "awaiting_first_complete_cycle"
    payload = {
        "status": status,
        "reason": reason,
        "epoch_state": epoch.get("state"),
        "last_update_age_seconds": evidence_age,
        "daemon_enabled": effective_alive,
        "daemon_configured": configured,
        "process_alive": process_alive,
        "heartbeat_stale": heartbeat_stale,
        "heartbeat_age_seconds": health.get("heartbeat_age_sec"),
        "activity_state": activity_state or None,
        "active_bot_count": active_count,
        "minimum_rating_pool_bots": health.get("minimum_rating_pool_bots", 2),
        "strength_evidence_available": evidence_available,
        "strength_evidence_status": evidence_status,
        "strength_evidence_reason": (
            None if evidence_available else snapshot.get("reason")
        ),
    }
    return payload


def _ratings_list_blocking() -> list:
    snapshot = _snapshot()
    return list(snapshot.get("selection_rows") or []) if snapshot.get("available") else []


@router.get("/ratings")
async def get_ratings():
    """Strength selection rows for the current strict evaluation cycle.

    Offloaded to an isolated worker thread: ``_snapshot`` reopens multiple
    JSON/JSONL files on each call and must not freeze the shared uvicorn event
    loop (single-worker, shared with the orchestrator).
    """
    return await run_blocking_isolated(
        _ratings_list_blocking,
        thread_name_prefix="ratings-snapshot",
    )


def _rating_detail_blocking(bot_name: str) -> dict:
    snapshot = _snapshot()
    for row in snapshot.get("selection_rows") or []:
        if row["name"] == bot_name:
            return row
    raise HTTPException(status_code=404, detail="Bot not found")


@router.get("/ratings/{bot_name}")
async def get_rating_detail(bot_name: str):
    """Per-bot rating detail for the current strict evaluation cycle.

    Offloaded to an isolated worker thread (see ``get_ratings``).
    """
    return await run_blocking_isolated(
        _rating_detail_blocking,
        bot_name,
        thread_name_prefix="ratings-snapshot",
    )


def _history_blocking(bots: str, resolution: str) -> list:
    entries = list(_snapshot().get("rating_history") or [])
    if resolution != "full" and len(entries) > 100:
        step = max(1, len(entries) // (200 if resolution == "medium" else 50))
        sampled = entries[::step]
        if entries[-1] not in sampled:
            sampled.append(entries[-1])
        entries = sampled
    bot_filter = set(b.strip() for b in bots.split(",") if b.strip()) if bots else None

    result = []
    for entry in entries:
        ratings = entry.get("ratings", {})
        win_rates = entry.get("win_rates", {})
        if bot_filter:
            ratings = {k: v for k, v in ratings.items() if k in bot_filter}
            win_rates = {k: v for k, v in win_rates.items() if k in bot_filter}
        result.append({
            "period": entry.get("period", 0),
            "timestamp": entry.get("timestamp", ""),
            "ratings": ratings,
            "win_rates": win_rates,
        })
    return result


@router.get("/history")
async def history(
    bots: str = Query("", description="Comma-separated bot names"),
    resolution: str = Query("medium", description="full, medium, or low"),
):
    """Rating history time series for the current strict evaluation cycle.

    Offloaded to an isolated worker thread: ``_snapshot`` reopens multiple
    JSON/JSONL files on each call and must not freeze the shared uvicorn event
    loop (single-worker, shared with the orchestrator).
    """
    return await run_blocking_isolated(
        _history_blocking,
        bots,
        resolution,
        thread_name_prefix="ratings-snapshot",
    )


def _history_summary_blocking() -> dict:
    entries = list(_snapshot().get("rating_history") or [])
    if not entries:
        return {}
    all_bots = set()
    for e in entries:
        all_bots.update(e.get("ratings", {}).keys())
    summary = {}
    for bot in sorted(all_bots):
        s = {}
        ratings = []
        for e in entries:
            bot_r = e.get("ratings", {}).get(bot)
            if not isinstance(bot_r, dict):
                continue
            r = bot_r.get("r")
            if r is not None:
                ratings.append(r)
        if ratings:
            s["peak_rating"] = round(max(ratings), 1)
            s["current_rating"] = round(ratings[-1], 1)
            s["trend"] = round(ratings[-1] - ratings[0], 1) if len(ratings) > 1 else 0
            s["periods"] = len(ratings)
        wr_list = [
            e["win_rates"][bot]["h2h_avg_wr"]
            for e in entries
            if bot in e.get("win_rates", {}) and e["win_rates"][bot].get("h2h_avg_wr") is not None
        ]
        if wr_list:
            s["peak_h2h_avg_wr"] = round(max(wr_list), 4)
            s["current_h2h_avg_wr"] = round(wr_list[-1], 4)
            s["wr_trend"] = round(wr_list[-1] - wr_list[0], 4) if len(wr_list) > 1 else 0
        if s:
            summary[bot] = s
    return summary


@router.get("/history/summary")
async def history_summary():
    """Aggregated rating/win-rate summary for the current strict evaluation cycle.

    Offloaded to an isolated worker thread (see ``history``).
    """
    return await run_blocking_isolated(
        _history_summary_blocking,
        thread_name_prefix="ratings-snapshot",
    )


def _daemon_status_blocking() -> dict:
    return strict_daemon_status()


@router.get("/daemon/status")
async def daemon_status():
    """Daemon observability projection for the current strict evaluation cycle.

    Offloaded to an isolated worker thread: ``strict_daemon_status`` transitively
    performs the strict epoch projection, a daemon health snapshot, the rating
    snapshot (multiple JSON/JSONL reads), and a manifest file stat. All of these
    are blocking file/process operations that must not freeze the shared
    uvicorn event loop (single-worker, shared with the orchestrator).
    """
    return await run_blocking_isolated(
        _daemon_status_blocking,
        thread_name_prefix="daemon-status",
    )


def _h2h_blocking(bot_name: str) -> dict:
    data = _snapshot().get("h2h") or {}
    if not data:
        return {}
    if not bot_name:
        return data
    filtered = {}
    for k, v in data.items():
        parts = k.split(" vs ")
        if bot_name in parts:
            filtered[k] = v
    return filtered


@router.get("/h2h")
async def get_h2h(bot_name: str = Query("", description="Filter by bot name")):
    """Head-to-head matrix for the current strict evaluation cycle.

    Offloaded to an isolated worker thread: ``_snapshot`` reopens multiple
    JSON/JSONL files on each call and must not freeze the shared uvicorn event
    loop (single-worker, shared with the orchestrator).
    """
    return await run_blocking_isolated(
        _h2h_blocking,
        bot_name,
        thread_name_prefix="ratings-snapshot",
    )


def _all_bot_stats_blocking() -> dict:
    return _snapshot().get("bot_stats") or {}


@router.get("/bot-stats")
async def get_all_bot_stats():
    """Per-bot aggregate stats for the current strict evaluation cycle.

    Offloaded to an isolated worker thread (see ``get_h2h``).
    """
    return await run_blocking_isolated(
        _all_bot_stats_blocking,
        thread_name_prefix="ratings-snapshot",
    )
