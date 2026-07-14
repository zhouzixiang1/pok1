"""Strict-epoch scheduler status and result API routes.

The retired scheduler journals predate the national TCP policy epoch. They are
an append-only transport, not an observability authority: rows without the
current epoch, evaluation identity, and published bot identities are treated
as retired bytes and are never projected to HTTP/SSE clients.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


def _empty_projection(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "reason": str(reason),
        "evaluation_epoch": None,
        "evaluation_identity_digest": None,
        "published_pool": [],
        "pending_jobs": 0,
        "claimed_jobs": 0,
        "recent_results": 0,
        "pending_details": [],
        "results": [],
    }


def _strict_scheduler_context() -> tuple[dict[str, Any] | None, str]:
    """Resolve the read-only authority shared by HTTP and SSE readers.

    Importantly, this uses the validating identity reader.  It never calls
    ``ensure_evaluation_data_identity`` and therefore a GET cannot initialize
    or repair runtime state.
    """

    try:
        from bot_namespace import EVALUATION_EPOCH
        from epoch_authority import policy_epoch_initialization
        from evaluation_bundle import validated_evaluation_identity_digest
        from server.routes._helpers import _strict_published_active_pool

        epoch = policy_epoch_initialization()
        if epoch.get("initialized") is not True:
            return None, str(epoch.get("state") or "policy_epoch_not_initialized")
        if epoch.get("evaluation_epoch") != EVALUATION_EPOCH:
            return None, "policy_epoch_identity_mismatch"

        identity = validated_evaluation_identity_digest()
        if (
            not isinstance(identity, str)
            or len(identity) != 64
            or any(char not in "0123456789abcdef" for char in identity)
        ):
            return None, "evaluation_identity_unavailable"

        published = _strict_published_active_pool()
        if not published:
            return None, "strict_published_pool_empty"
        return {
            "evaluation_epoch": EVALUATION_EPOCH,
            "evaluation_identity_digest": identity,
            "published_pool": tuple(published),
        }, "ok"
    except Exception as exc:
        return None, f"scheduler_authority_unavailable:{type(exc).__name__}"


def _strict_row(row: Any, context: dict[str, Any]) -> bool:
    """Admit one self-identifying row from the current published pool."""

    if not isinstance(row, dict):
        return False
    pool = set(context["published_pool"])
    bot_a = row.get("bot_a_name")
    bot_b = row.get("bot_b_name")
    return bool(
        row.get("evaluation_epoch") == context["evaluation_epoch"]
        and row.get("evaluation_identity_digest")
        == context["evaluation_identity_digest"]
        and bot_a in pool
        and bot_b in pool
        and bot_a != bot_b
        and isinstance(row.get("job_id"), str)
        and bool(row.get("job_id"))
    )


_JOB_FIELDS = (
    "job_id",
    "bot_a_name",
    "bot_b_name",
    "n_pairs",
    "submitted_at",
    "submitted_by",
    "priority",
    "timeout_sec",
    "update_ratings",
    "evaluation_epoch",
    "evaluation_identity_digest",
)
_RESULT_FIELDS = (
    "job_id",
    "bot_a_name",
    "bot_b_name",
    "wins_a",
    "wins_b",
    "draws",
    "total",
    "net_chips",
    "error",
    "completed_at",
    "source",
    "evaluation_epoch",
    "evaluation_identity_digest",
)


def _project_row(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Do not expose raw executable paths or unknown legacy payload fields."""

    return {field: row[field] for field in fields if field in row}


def strict_scheduler_projection(*, result_limit: int | None = None) -> dict[str, Any]:
    """Return the sole scheduler projection consumed by REST and SSE."""

    context, reason = _strict_scheduler_context()
    if context is None:
        # Stop before importing/reading the retired scheduler journals. This matters
        # during the one-time reset: those bytes belong to the retired epoch.
        return _empty_projection(reason)

    try:
        from battle_scheduler import (
            BATTLE_CLAIMED_FILE,
            BATTLE_JOBS_FILE,
            BATTLE_RESULTS_FILE,
            _read_jsonl,
        )

        pending = [
            row for row in _read_jsonl(BATTLE_JOBS_FILE)
            if _strict_row(row, context)
        ]
        claimed = [
            row for row in _read_jsonl(BATTLE_CLAIMED_FILE)
            if _strict_row(row, context)
        ]
        results = [
            row for row in _read_jsonl(BATTLE_RESULTS_FILE)
            if _strict_row(row, context)
        ]
    except Exception as exc:
        return _empty_projection(f"scheduler_queue_unavailable:{type(exc).__name__}")

    # Revalidate after I/O so an epoch reset, evaluation migration, or pool
    # publication that races this read cannot lend its old context to bytes
    # returned after the authority changed.
    current_context, current_reason = _strict_scheduler_context()
    if current_context != context:
        return _empty_projection(
            current_reason
            if current_context is None
            else "scheduler_authority_changed_during_read"
        )

    selected_results = results
    if result_limit is not None:
        selected_results = results[-max(0, int(result_limit)):] if result_limit > 0 else []
    return {
        "available": True,
        "reason": None,
        "evaluation_epoch": context["evaluation_epoch"],
        "evaluation_identity_digest": context["evaluation_identity_digest"],
        "published_pool": list(context["published_pool"]),
        "pending_jobs": len(pending),
        "claimed_jobs": len(claimed),
        "recent_results": len(results),
        "pending_details": [
            _project_row(row, _JOB_FIELDS) for row in pending[:5]
        ],
        "results": [
            _project_row(row, _RESULT_FIELDS) for row in selected_results
        ],
    }


@router.get("/status")
async def scheduler_status():
    """Return only current strict-identity scheduler queue status."""

    payload = strict_scheduler_projection(result_limit=0)
    payload.pop("results", None)
    return payload


@router.get("/results")
async def scheduler_results(limit: int = 20):
    """Return recent results that carry the exact current strict identity."""

    payload = strict_scheduler_projection(result_limit=limit)
    return {
        "available": payload["available"],
        "reason": payload["reason"],
        "evaluation_epoch": payload["evaluation_epoch"],
        "evaluation_identity_digest": payload["evaluation_identity_digest"],
        "published_pool": payload["published_pool"],
        "results": payload["results"],
    }
