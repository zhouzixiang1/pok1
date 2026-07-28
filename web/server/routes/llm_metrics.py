"""LLM call metrics endpoints — per-call timing, token, cost, and cache KPIs.

Reads ``llm_call_metrics.jsonl`` written by ``core/llm_call_metrics.py`` and
exposes three views: recent records, per-role aggregated summary, and a full
JSONL export for offline analysis.
"""

import json

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from blocking_runtime import run_blocking_isolated
from evolution_infra import RESULTS_DIR, locked_file

METRICS_FILE = RESULTS_DIR / "llm_call_metrics.jsonl"

router = APIRouter(prefix="/api/llm", tags=["llm-metrics"])

# Numeric fields aggregated in /metrics/summary.  ``avg_max`` fields produce
# both average and max; ``total_only`` fields only produce a sum.
_AVG_MAX_FIELDS = (
    "total_elapsed_sec",
    "first_token_latency_sec",
    "first_text_latency_sec",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "thinking_tokens_estimated",
    "output_tokens_per_sec",
    "cost_usd",
)
_TOTAL_ONLY_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd",
)


def _read_metrics_lines() -> list[dict]:
    """Read and parse the whole metrics JSONL file under a shared lock.

    Returns an empty list if the file is missing, empty, or unreadable.
    Lines that fail to parse as JSON are silently skipped, matching the
    best-effort write contract in ``record_llm_call_metrics``.
    """
    if not METRICS_FILE.exists():
        return []
    rows: list[dict] = []
    try:
        with locked_file(METRICS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def _get_metrics_blocking(limit: int) -> list[dict]:
    rows = _read_metrics_lines()
    rows.reverse()  # newest first
    return rows[:limit]


@router.get("/metrics")
async def get_metrics(
    limit: int = Query(200, ge=1, le=1000, description="Number of most recent records"),
):
    """Return the most recent N LLM call metric records (newest first).

    Offloaded to an isolated worker thread: ``_read_metrics_lines`` reads and
    parses the entire ``llm_call_metrics.jsonl`` under a shared lock — blocking
    work that must not freeze the shared uvicorn event loop (single-worker,
    shared with the orchestrator).
    """
    return await run_blocking_isolated(
        _get_metrics_blocking,
        limit,
        thread_name_prefix="llm-metrics",
    )


def _get_metrics_summary_blocking() -> dict:
    rows = _read_metrics_lines()
    if not rows:
        return {"roles": {}, "total": {}}

    def _empty_bucket() -> dict:
        bucket = {
            "calls": 0,
            "successes": 0,
            "cache_read_tokens": 0,
            "cache_eligible_tokens": 0,
        }
        for field in _AVG_MAX_FIELDS:
            bucket[f"{field}_sum"] = 0.0
            bucket[f"{field}_count"] = 0
            bucket[f"{field}_max"] = None
        for field in _TOTAL_ONLY_FIELDS:
            bucket[f"{field}_total"] = 0.0
        return bucket

    def _accumulate(bucket: dict, row: dict) -> None:
        bucket["calls"] += 1
        if row.get("success") is True:
            bucket["successes"] += 1
        cache_read = row.get("cache_read_input_tokens") or 0
        cache_write = row.get("cache_creation_input_tokens") or 0
        input_tok = row.get("input_tokens") or 0
        bucket["cache_read_tokens"] += cache_read
        bucket["cache_eligible_tokens"] += cache_read + cache_write + input_tok
        for field in _AVG_MAX_FIELDS:
            value = row.get(field)
            if value is None:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            bucket[f"{field}_sum"] += value
            bucket[f"{field}_count"] += 1
            current_max = bucket[f"{field}_max"]
            if current_max is None or value > current_max:
                bucket[f"{field}_max"] = value
        for field in _TOTAL_ONLY_FIELDS:
            value = row.get(field)
            if value is None:
                continue
            try:
                bucket[f"{field}_total"] += float(value)
            except (TypeError, ValueError):
                continue

    def _finalize(bucket: dict) -> dict:
        calls = bucket["calls"]
        successes = bucket["successes"]
        cache_read = bucket["cache_read_tokens"]
        cache_eligible = bucket["cache_eligible_tokens"]
        result = {
            "calls": calls,
            "success_rate": round(successes / calls, 4) if calls else None,
            "cache_hit_rate": (
                round(cache_read / cache_eligible, 4) if cache_eligible else None
            ),
        }
        for field in _AVG_MAX_FIELDS:
            count = bucket[f"{field}_count"]
            result[f"{field}_avg"] = (
                round(bucket[f"{field}_sum"] / count, 4) if count else None
            )
            result[f"{field}_max"] = (
                round(bucket[f"{field}_max"], 4)
                if bucket[f"{field}_max"] is not None
                else None
            )
            del bucket[f"{field}_sum"]
            del bucket[f"{field}_count"]
            del bucket[f"{field}_max"]
        for field in _TOTAL_ONLY_FIELDS:
            result[f"{field}_total"] = round(bucket[f"{field}_total"], 4)
        return result

    roles: dict[str, dict] = {}
    total = _empty_bucket()
    for row in rows:
        _accumulate(total, row)
        role = row.get("role") or "(unknown)"
        bucket = roles.get(role)
        if bucket is None:
            bucket = _empty_bucket()
            roles[role] = bucket
        _accumulate(bucket, row)

    return {
        "roles": {role: _finalize(b) for role, b in roles.items()},
        "total": _finalize(total),
    }


@router.get("/metrics/summary")
async def get_metrics_summary():
    """Aggregate metrics grouped by ``role``.

    Each role bucket reports call count, success rate, cache hit rate, plus
    average / max / total for the core numeric KPIs (tokens, elapsed seconds,
    throughput, cost).  A ``__total__`` bucket rolls up every role.

    Offloaded to an isolated worker thread: the full JSONL read + Python
    aggregation are blocking work that must not freeze the shared uvicorn event
    loop (single-worker, shared with the orchestrator).
    """
    return await run_blocking_isolated(
        _get_metrics_summary_blocking,
        thread_name_prefix="llm-metrics",
    )


def _export_metrics_blocking() -> str | None:
    """Synchronous raw JSONL read for offloaded execution.

    Returns the file content, or ``None`` when the metrics file is missing or
    unreadable. Runs on an isolated worker thread (see ``export_metrics``) so
    the full-file read under a shared file lock never freezes the event loop.
    """
    if not METRICS_FILE.exists():
        return None
    try:
        with locked_file(METRICS_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


@router.get("/metrics/export", response_class=PlainTextResponse)
async def export_metrics():
    """Return the raw JSONL content of the metrics file for export.

    The response is ``application/x-ndjson`` so external tools can stream it
    directly.  Missing file yields an empty body.

    Offloaded to an isolated worker thread: the full-file read under a shared
    file lock is blocking work that must not freeze the shared uvicorn event
    loop (single-worker, shared with the orchestrator).
    """
    content = await run_blocking_isolated(
        _export_metrics_blocking,
        thread_name_prefix="llm-metrics",
    )
    if content is None:
        return PlainTextResponse("", media_type="application/x-ndjson")
    return PlainTextResponse(content, media_type="application/x-ndjson")
