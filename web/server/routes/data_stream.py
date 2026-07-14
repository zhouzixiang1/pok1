"""Global data SSE stream — pushes all dashboard data on scheduled intervals."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
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


def _event(event_type: str, data: Any) -> dict:
    return {"event": event_type, "data": json.dumps(data, default=str)}


def _epoch_projection() -> dict:
    """Return the live authority used to fence one SSE connection."""

    try:
        from epoch_authority import strict_epoch_projection

        value = strict_epoch_projection(include_checkpoint=False)
    except Exception:
        value = {}
    return {
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_state": str(value.get("state") or "epoch_authority_unavailable"),
        "epoch_initialized": value.get("initialized") is True,
        "epoch_reset_receipt_digest": value.get("reset_receipt_digest"),
    }


def _strict_snapshot() -> dict:
    from server.routes._helpers import load_strict_strength_snapshot

    snapshot = load_strict_strength_snapshot(RESULTS_DIR)
    return snapshot if snapshot.get("available") is True else {}


def _get_ratings(snapshot: dict | None = None) -> list[dict]:
    snapshot = snapshot if snapshot is not None else _strict_snapshot()
    return list(snapshot.get("selection_rows") or [])


def _get_daemon_status(snapshot: dict | None = None) -> dict:
    from server.routes.ratings import strict_daemon_status

    snapshot = snapshot if snapshot is not None else _strict_snapshot()
    return strict_daemon_status(snapshot)


def _get_bots(snapshot: dict | None = None) -> dict:
    from server.routes.bots import (
        _inventory_strength_snapshot,
        _strict_published_inventory,
        build_bot_listing,
    )

    active_names = _strict_published_inventory()
    if snapshot is None or set(snapshot.get("active_bots") or []) != set(active_names):
        snapshot = _inventory_strength_snapshot(active_names)
    return build_bot_listing(
        snapshot.get("ratings") or {},
        snapshot.get("bot_stats") or {},
        snapshot.get("h2h") or {},
        include_history=False,
        active_names=active_names,
        strength_rows_data=snapshot.get("selection_rows") or [],
        strength_evidence_available=bool(snapshot),
    )


def _get_match_stats(snapshot: dict | None = None) -> dict:
    from server.routes._helpers import build_match_stats
    snapshot = snapshot if snapshot is not None else _strict_snapshot()
    return build_match_stats(snapshot.get("daemon_stats"))


def _get_recent_matches(limit: int = 100, snapshot: dict | None = None) -> list[dict]:
    snapshot = snapshot if snapshot is not None else _strict_snapshot()
    return list(snapshot.get("match_history") or [])[:limit]


def _get_match_matrix(snapshot: dict | None = None) -> dict:
    from server.routes._helpers import build_match_matrix
    snapshot = snapshot if snapshot is not None else _strict_snapshot()
    return build_match_matrix(
        snapshot.get("h2h") or {},
        snapshot.get("ratings") or {},
        snapshot.get("daemon_stats") or {},
    )


def _get_h2h(snapshot: dict | None = None) -> dict:
    snapshot = snapshot if snapshot is not None else _strict_snapshot()
    return snapshot.get("h2h") or {}


def _get_bot_stats(snapshot: dict | None = None) -> dict:
    snapshot = snapshot if snapshot is not None else _strict_snapshot()
    return snapshot.get("bot_stats") or {}


def _get_history(snapshot: dict | None = None) -> list[dict]:
    snapshot = snapshot if snapshot is not None else _strict_snapshot()
    return list(snapshot.get("rating_history") or [])


def _downsample(entries: list[dict], max_points: int = 200) -> list[dict]:
    from server.routes._helpers import downsample
    return downsample(entries, max_points)


def _get_generations() -> list[dict]:
    from server.routes._helpers import (
        list_generation_dirs,
        strict_observable_generation_versions,
    )

    allowed = strict_observable_generation_versions(
        RESULTS_DIR,
        RESULTS_DIR / "pipeline_state.json",
    )
    return list_generation_dirs(RESULTS_DIR, allowed_versions=allowed)


_log = logging.getLogger("data_stream")


@router.get("/data/stream")
async def data_stream(request: Request):
    async def generate():
        tick = 0
        connection_receipt_digest: str | None = None
        try:
            while True:
                if await request.is_disconnected():
                    break
                epoch = _epoch_projection()
                receipt_digest = epoch.get("epoch_reset_receipt_digest")
                if connection_receipt_digest is None and epoch["epoch_initialized"]:
                    connection_receipt_digest = (
                        str(receipt_digest) if receipt_digest else None
                    )
                if (
                    not epoch["epoch_initialized"]
                    or not connection_receipt_digest
                    or receipt_digest != connection_receipt_digest
                ):
                    # Close the browser cache at the same boundary that makes
                    # the server-side evidence projection unavailable or moves
                    # it to a different reset receipt.
                    yield _event("epoch_blocked", epoch)
                    break
                snapshot = _strict_snapshot()
                if tick % 3 == 0:
                    try:
                        events = [
                            _event("ratings", _get_ratings(snapshot)),
                            _event("daemon", _get_daemon_status(snapshot)),
                            _event("bots", _get_bots(snapshot)),
                            _event("stats", _get_match_stats(snapshot)),
                        ]
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
                            _event("matches", _get_recent_matches(100, snapshot)),
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
                            _event("matrix", _get_match_matrix(snapshot)),
                            _event("h2h", _get_h2h(snapshot)),
                            _event("bot_stats", _get_bot_stats(snapshot)),
                            _event("history", _downsample(_get_history(snapshot))),
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
