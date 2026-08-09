"""LLM call metrics endpoints — per-call timing, token, cost, and cache KPIs.

Reads ``llm_call_metrics.jsonl`` written by ``core/llm_call_metrics.py`` and
exposes five views: recent records, per-role aggregated summary (with a
``by_role`` array shape for the dashboard), per-generation aggregation, a
real-time live-utilization snapshot, and a full JSONL export for offline
analysis.
"""

import json
import time

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from blocking_runtime import run_blocking_isolated
from evolution_infra import RESULTS_DIR, locked_file
from server.cache import cached_by_mtime

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
    limit: int = Query(50, ge=1, le=1000, description="Number of most recent records"),
):
    """Return the most recent N LLM call metric records (newest first).

    Defaults to 50 records so the common dashboard poll stays light; clients
    that need more history pass an explicit larger ``limit`` (up to 1000).
    The upper bound is unchanged, so a request for 200 records still works.

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
    # mtime-keyed cache: the summary rereads + re-aggregates the whole JSONL on
    # every poll, and the file only changes when the daemon appends a new LLM
    # call record. Keying on the metrics file mtime auto-invalidates the moment
    # new data lands; the 2s TTL caps staleness within a poll burst.
    return cached_by_mtime(
        "llm_metrics:summary",
        METRICS_FILE,
        _compute_metrics_summary,
    )


def _compute_metrics_summary() -> dict:
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

    finalized_roles = {role: _finalize(b) for role, b in roles.items()}
    finalized_total = _finalize(total)
    return {
        "roles": finalized_roles,
        # ``by_role`` mirrors the frontend ``LlmRoleSummary`` shape so the
        # dashboard consumes the backend-aggregated values directly instead of
        # re-deriving them client-side. Kept alongside the legacy ``roles`` dict
        # for backwards-compat; both reflect the same underlying buckets.
        "by_role": _roles_to_by_role(finalized_roles),
        "total": finalized_total,
    }


def _roles_to_by_role(roles: dict[str, dict]) -> list[dict]:
    """Convert the finalized per-role dict into the ``LlmRoleSummary`` array.

    Each entry is: ``role``, ``count``, ``success_count``, ``success_rate``,
    ``avg_total_elapsed_sec``, ``max_total_elapsed_sec``, ``avg_total_tokens``,
    ``avg_first_token_latency_sec``, ``total_cost_usd``.  ``count`` is the
    canonical field name (the legacy dict key is ``calls``); both carry the
    same value.  ``success_count`` is derived from ``calls * success_rate`` and
    rounded to an exact integer so the frontend never displays a fractional
    call count.
    """
    out: list[dict] = []
    for role, b in roles.items():
        calls = b.get("calls") or 0
        success_rate = b.get("success_rate")
        out.append({
            "role": role,
            "count": calls,
            "success_count": (
                round(calls * success_rate) if success_rate is not None else 0
            ),
            "success_rate": success_rate if success_rate is not None else 0,
            "avg_total_elapsed_sec": b.get("total_elapsed_sec_avg"),
            "max_total_elapsed_sec": b.get("total_elapsed_sec_max"),
            "avg_total_tokens": b.get("total_tokens_avg"),
            "avg_first_token_latency_sec": b.get("first_token_latency_sec_avg"),
            "total_cost_usd": b.get("cost_usd_total"),
        })
    out.sort(key=lambda r: r["count"], reverse=True)
    return out


@router.get("/metrics/summary")
async def get_metrics_summary():
    """Aggregate metrics grouped by ``role``.

    Each role bucket reports call count, success rate, cache hit rate, plus
    average / max / total for the core numeric KPIs (tokens, elapsed seconds,
    throughput, cost).  A ``__total__`` bucket rolls up every role.  A
    ``by_role`` array (same data, ``LlmRoleSummary`` shape) is also published
    for direct frontend consumption.

    Offloaded to an isolated worker thread: the full JSONL read + Python
    aggregation are blocking work that must not freeze the shared uvicorn
    event loop (single-worker, shared with the orchestrator).
    """
    return await run_blocking_isolated(
        _get_metrics_summary_blocking,
        thread_name_prefix="llm-metrics",
    )


_LIVE_RECENT_WINDOW_SEC = 300.0


def _get_metrics_live_blocking() -> dict:
    """Snapshot real-time utilization + recent call count.

    The active-stream count comes from the shared LLM semaphore (capacity minus
    currently-available permits); the recent-calls count scans the JSONL for
    rows whose ``epoch_ts`` (or ``ts``) falls inside the last
    ``_LIVE_RECENT_WINDOW_SEC`` seconds.  The file read is the same append-only
    JSONL already read by the other endpoints, so it stays cheap under the
    shared file lock; the mtime guard in ``cached_by_mtime`` is not used here
    because the active-stream count is live process state that must not be
    cached across polls.
    """
    from llm_concurrency import get_active_stream_count, get_capacity

    capacity = get_capacity()
    active = get_active_stream_count()

    cutoff = time.time() - _LIVE_RECENT_WINDOW_SEC
    recent_calls = 0
    rows = _read_metrics_lines()
    for row in rows:
        ts = row.get("epoch_ts")
        if ts is None:
            ts_str = row.get("ts")
            if ts_str:
                try:
                    ts = time.mktime(time.strptime(ts_str, "%Y-%m-%dT%H:%M:%S"))
                except (ValueError, TypeError):
                    ts = None
        if ts is None:
            continue
        try:
            if float(ts) >= cutoff:
                recent_calls += 1
        except (TypeError, ValueError):
            continue

    return {
        "active_streams": active,
        "capacity": capacity,
        "utilization_pct": (
            round(active / capacity * 100, 1) if capacity > 0 else 0
        ),
        "recent_calls_5min": recent_calls,
        "timestamp": time.time(),
    }


@router.get("/metrics/live")
async def get_metrics_live():
    """Real-time LLM status: in-flight streams, capacity, and recent call rate.

    ``active_streams`` is an instantaneous read of the shared semaphore
    (capacity minus available permits).  ``recent_calls_5min`` counts JSONL
    records in the trailing 5-minute window.

    The active-stream read is a fast in-process integer; the recent-calls
    count is offloaded to an isolated worker thread because it scans the whole
    JSONL under a shared file lock and must not freeze the event loop.
    """
    return await run_blocking_isolated(
        _get_metrics_live_blocking,
        thread_name_prefix="llm-metrics",
    )


def _get_metrics_by_generation_blocking() -> list[dict]:
    return cached_by_mtime(
        "llm_metrics:by_generation",
        METRICS_FILE,
        _compute_metrics_by_generation,
    )


def _compute_metrics_by_generation() -> list[dict]:
    """Aggregate per-call records by ``generation_id``.

    Rows without a ``generation_id`` are skipped.  Each output entry reports
    ``generation_id``, ``calls``, ``total_cost``, ``avg_elapsed``, plus input /
    output / thinking token sums.  Sorted by ``generation_id`` descending so the
    most recent generation appears first.
    """
    rows = _read_metrics_lines()
    buckets: dict[str, dict] = {}
    for row in rows:
        gen = row.get("generation_id")
        if not gen:
            continue
        bucket = buckets.get(gen)
        if bucket is None:
            bucket = {
                "generation_id": gen,
                "calls": 0,
                "total_cost": 0.0,
                "cost_count": 0,
                "elapsed_sum": 0.0,
                "elapsed_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "thinking_tokens": 0,
            }
            buckets[gen] = bucket
        bucket["calls"] += 1
        cost = row.get("cost_usd")
        if cost is not None:
            try:
                bucket["total_cost"] += float(cost)
                bucket["cost_count"] += 1
            except (TypeError, ValueError):
                pass
        elapsed = row.get("total_elapsed_sec")
        if elapsed is not None:
            try:
                bucket["elapsed_sum"] += float(elapsed)
                bucket["elapsed_count"] += 1
            except (TypeError, ValueError):
                pass
        for src_field, dst_field in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("thinking_tokens_estimated", "thinking_tokens"),
        ):
            val = row.get(src_field)
            if val is None:
                continue
            try:
                bucket[dst_field] += float(val)
            except (TypeError, ValueError):
                continue

    out: list[dict] = []
    for gen, b in buckets.items():
        out.append({
            "generation_id": gen,
            "calls": b["calls"],
            "total_cost": round(b["total_cost"], 6),
            "avg_elapsed": (
                round(b["elapsed_sum"] / b["elapsed_count"], 3)
                if b["elapsed_count"] else 0
            ),
            "input_tokens": int(round(b["input_tokens"])),
            "output_tokens": int(round(b["output_tokens"])),
            "thinking_tokens": int(round(b["thinking_tokens"])),
        })
    out.sort(key=lambda r: r["generation_id"], reverse=True)
    return out


@router.get("/metrics/by-generation")
async def get_metrics_by_generation():
    """Aggregate LLM calls grouped by ``generation_id``.

    Returns one row per generation with call count, total cost, average
    elapsed seconds, and input/output/thinking token sums.  Rows with no
    ``generation_id`` are excluded.  Sorted by ``generation_id`` descending.

    Offloaded to an isolated worker thread: the full JSONL read + Python
    aggregation are blocking work that must not freeze the shared uvicorn
    event loop (single-worker, shared with the orchestrator).
    """
    return await run_blocking_isolated(
        _get_metrics_by_generation_blocking,
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
