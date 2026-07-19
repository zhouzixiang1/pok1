"""Global data SSE stream — pushes all dashboard data on scheduled intervals."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse
from blocking_runtime import run_blocking_isolated

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
    """Return the shared content-bound observer authority for one SSE tick.

    The control observer owns the complete strict projection singleflight.
    Reusing it here prevents a single data-stream tick from reopening every
    official certificate/verdict ledger once per emitted event.  This helper
    is read-only; launch and mutation paths bypass the observer cache.
    """

    try:
        from server.routes.control import control_observer_epoch_projection

        return control_observer_epoch_projection()
    except Exception:
        return {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "epoch_authority_unavailable",
            "initialized": False,
            "reset_receipt_valid": False,
            "reset_receipt_digest": None,
            "version_authority_high_water": None,
            "active_bots": [],
            "strict_published_bot_identities": [],
            "epoch_state": "epoch_authority_unavailable",
            "epoch_initialized": False,
            "epoch_reset_receipt_digest": None,
            "stream_authority_digest": None,
        }


def _strict_snapshot() -> dict:
    from server.routes._helpers import load_strict_strength_snapshot

    snapshot = load_strict_strength_snapshot(RESULTS_DIR)
    return snapshot if snapshot.get("available") is True else {}


def _get_ratings(snapshot: dict | None = None) -> list[dict]:
    snapshot = snapshot if snapshot is not None else _strict_snapshot()
    return list(snapshot.get("selection_rows") or [])


def _get_daemon_status(
    snapshot: dict | None = None,
    epoch: dict | None = None,
) -> dict:
    from server.routes.ratings import strict_daemon_status

    snapshot = snapshot if snapshot is not None else _strict_snapshot()
    return strict_daemon_status(snapshot, epoch=epoch)


def _get_bots(
    snapshot: dict | None = None,
    epoch: dict | None = None,
) -> dict:
    from server.routes.bots import (
        _inventory_strength_snapshot,
        _strict_published_authority,
        build_bot_listing,
    )

    active_names, generation_identities = _strict_published_authority(epoch)
    if snapshot is None or set(snapshot.get("active_bots") or []) != set(active_names):
        snapshot = _inventory_strength_snapshot(active_names)
    return build_bot_listing(
        snapshot.get("ratings") or {},
        snapshot.get("bot_stats") or {},
        snapshot.get("h2h") or {},
        include_history=False,
        active_names=active_names,
        generation_identities=generation_identities,
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


def _data_tick_content_key() -> tuple:
    """Return the local content identity which fences one complete SSE tick."""

    from server.routes.control import _observer_authority_content_key
    from server.routes.pipeline import _path_stat_token

    return (
        _observer_authority_content_key(),
        _path_stat_token(RESULTS_DIR / "evaluation_cycle_manifest.json"),
        _path_stat_token(RESULTS_DIR / "pipeline_state.json"),
    )


def _build_data_tick(
    tick: int,
    expected_authority_digest: str,
    expected_identity_valid: bool,
) -> tuple[dict, list[dict]]:
    """Build one content-bound dashboard tick outside the ASGI loop.

    All events in the returned batch share one complete epoch and strength
    observation.  Local authority movement during the build withholds the
    whole batch, so no individual event can escape from a torn observation.
    """

    before = _data_tick_content_key()
    epoch = _epoch_projection()
    events: list[dict] = []
    if (
        not epoch.get("epoch_initialized")
        or not expected_identity_valid
        or epoch.get("stream_authority_digest") != expected_authority_digest
    ):
        return epoch, events

    needs_snapshot = tick % 3 == 0 or tick % 10 == 0 or tick % 15 == 0
    snapshot = _strict_snapshot() if needs_snapshot else {}
    if tick % 3 == 0:
        try:
            events.extend((
                _event("ratings", _get_ratings(snapshot)),
                _event("daemon", _get_daemon_status(snapshot, epoch)),
                _event("bots", _get_bots(snapshot, epoch)),
                _event("stats", _get_match_stats(snapshot)),
            ))
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
        except Exception as exc:
            _log.warning("SSE data fetch error (3s): %s", exc)
    if tick % 10 == 0:
        try:
            events.extend((
                _event("matches", _get_recent_matches(100, snapshot)),
                _event("generations", _get_generations()),
            ))
        except Exception as exc:
            _log.warning("SSE data fetch error (10s): %s", exc)
    if tick % 15 == 0:
        try:
            events.extend((
                _event("matrix", _get_match_matrix(snapshot)),
                _event("h2h", _get_h2h(snapshot)),
                _event("bot_stats", _get_bot_stats(snapshot)),
                _event("history", _downsample(_get_history(snapshot))),
            ))
        except Exception as exc:
            _log.warning("SSE data fetch error (15s): %s", exc)
    if tick % 30 == 0:
        events.append({"event": "ping", "data": "{}"})

    after = _data_tick_content_key()
    if after != before:
        return {
            **epoch,
            "state": "epoch_authority_unavailable",
            "initialized": False,
            "epoch_state": "epoch_authority_unavailable",
            "epoch_initialized": False,
            "stream_authority_digest": None,
            "authority_issue": "data_tick_authority_changed_during_build",
        }, []
    return epoch, events


async def _build_data_tick_async(
    tick: int,
    expected_authority_digest: str,
    expected_identity_valid: bool,
) -> tuple[dict, list[dict]]:
    """Run the complete content-bound tick outside the shared ASGI loop."""

    return await run_blocking_isolated(
        _build_data_tick,
        tick,
        expected_authority_digest,
        expected_identity_valid,
        thread_name_prefix="data-stream-tick",
    )


@router.get("/data/stream")
async def data_stream(request: Request):
    expected_authority_digest = str(
        request.query_params.get("authority") or ""
    )
    expected_identity_valid = bool(
        len(expected_authority_digest) == 64
        and all(char in "0123456789abcdef" for char in expected_authority_digest)
    )

    async def generate():
        tick = 0
        try:
            while True:
                if await request.is_disconnected():
                    break
                epoch, events = await _build_data_tick_async(
                    tick,
                    expected_authority_digest,
                    expected_identity_valid,
                )
                authority_digest = epoch.get("stream_authority_digest")
                if (
                    not epoch["epoch_initialized"]
                    or not expected_identity_valid
                    or authority_digest != expected_authority_digest
                ):
                    # Close the browser cache at the same boundary that makes
                    # the server-side evidence projection unavailable or moves
                    # it to a different reset/publication identity.
                    yield _event("epoch_blocked", epoch)
                    break
                # The complete batch was bracketed by one local content key;
                # every row carries the connection's already-matched epoch.
                for evt in events:
                    try:
                        yield evt
                    except Exception as exc:
                        _log.warning("SSE event error: %s", exc)
                await asyncio.sleep(1)
                tick += 1
        except asyncio.CancelledError:
            pass
    return EventSourceResponse(generate())
