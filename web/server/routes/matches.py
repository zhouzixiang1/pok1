"""Match endpoints for national-native strength evidence and replays."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from blocking_runtime import run_blocking_isolated
from server.cache import read_locked
from server.routes._helpers import (
    build_match_matrix,
    build_match_stats,
    load_strict_strength_snapshot,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = PROJECT_ROOT / "web" / "core" / "results"
STATS_FILE = RESULTS_DIR / "elo_daemon_stats.json"
RATINGS_FILE = RESULTS_DIR / "glicko_ratings.json"
H2H_FILE = RESULTS_DIR / "head_to_head.json"
REPLAY_DIR = RESULTS_DIR / "match_replay"
MATCH_HISTORY_FILE = RESULTS_DIR / "match_history.jsonl"

router = APIRouter(prefix="/api", tags=["matches"])


def _snapshot() -> dict:
    snapshot = load_strict_strength_snapshot(RESULTS_DIR)
    return snapshot if snapshot.get("available") is True else {}


def _match_matrix_blocking() -> dict:
    snapshot = _snapshot()
    return build_match_matrix(
        snapshot.get("h2h") or {},
        snapshot.get("ratings") or {},
        snapshot.get("daemon_stats") or {},
    )


@router.get("/matches/matrix")
async def match_matrix():
    """Head-to-head matrix + ratings + daemon stats for the current strict cycle.

    Offloaded to an isolated worker thread: ``_snapshot`` reopens multiple
    JSON/JSONL files on each call and must not freeze the shared uvicorn event
    loop (single-worker, shared with the orchestrator).
    """
    return await run_blocking_isolated(
        _match_matrix_blocking,
        thread_name_prefix="matches-matrix",
    )


def _match_stats_blocking() -> dict:
    return build_match_stats(_snapshot().get("daemon_stats"))


@router.get("/matches/stats")
async def match_stats():
    """Aggregate match stats for the current strict evaluation cycle.

    Offloaded to an isolated worker thread (see ``match_matrix``).
    """
    return await run_blocking_isolated(
        _match_stats_blocking,
        thread_name_prefix="matches-matrix",
    )


def _recent_matches_blocking(limit: int) -> list:
    return list(_snapshot().get("match_history") or [])[:limit]


@router.get("/matches/recent")
async def recent_matches(limit: int = Query(50, le=200)):
    """Most recent admitted matches for the current strict evaluation cycle.

    Offloaded to an isolated worker thread (see ``match_matrix``).
    """
    return await run_blocking_isolated(
        _recent_matches_blocking,
        limit,
        thread_name_prefix="matches-matrix",
    )


def _match_replay_blocking(match_id: str) -> dict:
    """Synchronous strict-replay admission + read for offloaded execution.

    Runs on an isolated worker thread (see ``match_replay``) so the snapshot
    read, locked replay read, and native replay validation never freeze the
    shared uvicorn event loop.
    """
    path = (REPLAY_DIR / match_id).resolve()
    if not path.is_relative_to(REPLAY_DIR.resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail="Match not found")
    snapshot = _snapshot()
    admitted_match = next(
        (
            row
            for row in (snapshot.get("match_history") or [])
            if isinstance(row, dict) and row.get("id") == match_id
        ),
        None,
    )
    if admitted_match is None:
        raise HTTPException(
            status_code=409,
            detail="Replay is not admitted by the current strict evaluation cycle",
        )
    try:
        replay = read_locked(path)
        from replay_analysis import validate_native_replay

        identity = snapshot["evaluation_identity_digest"]
        validation = validate_native_replay(
            replay,
            expected_evaluation_identity_digest=identity,
            expected_replay_id=match_id,
        )
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Current national replay identity is unavailable",
        ) from None
    if not validation.accepted:
        raise HTTPException(
            status_code=409,
            detail=f"Replay is not current national_tcp_policy_v1 evidence: {validation.reason}",
        )
    admitted_players = (admitted_match.get("bot0"), admitted_match.get("bot1"))
    replay_players = (replay.get("bot0"), replay.get("bot1"))
    active_bots = set(snapshot.get("active_bots") or [])
    if replay_players != admitted_players or not set(replay_players).issubset(active_bots):
        raise HTTPException(
            status_code=409,
            detail="Replay players are not the current published match identity",
        )
    return replay


@router.get("/matches/replay/{match_id}")
async def match_replay(match_id: str):
    """Validate + read an admitted replay for the current strict evaluation cycle.

    Offloaded to an isolated worker thread: the snapshot read, locked replay
    read (``read_locked``), and ``validate_native_replay`` are blocking file +
    CPU operations that must not freeze the shared uvicorn event loop.
    """
    return await run_blocking_isolated(
        _match_replay_blocking,
        match_id,
        thread_name_prefix="matches-replay",
    )
