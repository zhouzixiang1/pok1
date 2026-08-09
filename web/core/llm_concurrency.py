"""Global LLM concurrency limiter (single shared pool).

All sub-agent LLM calls funnel through ``run_claude_query`` which acquires a
semaphore before dispatching to the provider.  This caps the number of
simultaneous in-flight LLM streams.

**Single shared pool.**  All roles (Master Scouts/Critics/final, Workers,
direction audit, review, critic) share one FIFO semaphore.  The former
producer/consumer hard partition (``POK_LLM_CONSUMER_CONCURRENCY`` /
``PRODUCER_LLM_CONCURRENCY``) left slots idle because the pipeline stages are
temporally separated: during the Master/Worker phase the consumer sub-pool's
slots sat empty, and during the gate phase the producer sub-pool's slots sat
empty.  With a single shared pool, every permit is available to whichever role
actually has work, which roughly doubles real-world utilization for the same
permit count.

FIFO ordering (``asyncio.Semaphore`` is deque-backed) prevents starvation: no
role is permanently blocked, and the gate-stage roles (review/critic) compete
on equal footing with producer roles.  The gate stages make far fewer LLM
calls than Master/Workers, so starvation is not a practical risk.

The legacy partitioned getters (``get_consumer_llm_semaphore`` /
``get_producer_llm_semaphore``) are retained as backwards-compat aliases that
all return the same shared semaphore, so existing imports keep resolving.
"""

import asyncio
import os

# Total concurrent in-flight LLM streams across ALL roles.
GLOBAL_LLM_CONCURRENCY = int(os.environ.get("POK_GLOBAL_LLM_CONCURRENCY", "4"))

# Legacy env vars retained for backwards compat but no longer partition.
_CONSUMER_LLM_CONCURRENCY = max(1, GLOBAL_LLM_CONCURRENCY // 3)
PRODUCER_LLM_CONCURRENCY = max(1, GLOBAL_LLM_CONCURRENCY - _CONSUMER_LLM_CONCURRENCY)
# Kept as a module-level constant for any code that still reads it.
CONSUMER_LLM_CONCURRENCY = GLOBAL_LLM_CONCURRENCY

_SHARED_LLM_SEMAPHORE: "asyncio.Semaphore | None" = None
# Legacy single-pool alias (same object).
_GLOBAL_LLM_SEMAPHORE: "asyncio.Semaphore | None" = None


def _get_shared_semaphore() -> asyncio.Semaphore:
    global _SHARED_LLM_SEMAPHORE, _GLOBAL_LLM_SEMAPHORE
    if _SHARED_LLM_SEMAPHORE is None:
        _SHARED_LLM_SEMAPHORE = asyncio.Semaphore(GLOBAL_LLM_CONCURRENCY)
        _GLOBAL_LLM_SEMAPHORE = _SHARED_LLM_SEMAPHORE
    return _SHARED_LLM_SEMAPHORE


def get_global_llm_semaphore() -> asyncio.Semaphore:
    """Return the single shared LLM dispatch semaphore."""
    return _get_shared_semaphore()


def get_consumer_llm_semaphore() -> asyncio.Semaphore:
    """Legacy alias — returns the shared semaphore (no longer partitioned)."""
    return _get_shared_semaphore()


def get_producer_llm_semaphore() -> asyncio.Semaphore:
    """Legacy alias — returns the shared semaphore (no longer partitioned)."""
    return _get_shared_semaphore()


def get_llm_semaphore_for_role(role_name: str | None) -> asyncio.Semaphore:
    """Return the shared semaphore for any role.

    All roles share one FIFO pool. The former producer/consumer partition left
    slots idle during temporally-separated pipeline phases (Master/Workers vs
    gates); a single pool lets every permit fill whichever role has work,
    roughly doubling real utilization for the same permit count.
    """
    return _get_shared_semaphore()


def get_active_stream_count() -> int:
    """Approximate count of currently in-use LLM permits (capacity - available).

    This is an instantaneous read of ``capacity - semaphore._value``. It is an
    approximation: a permit that was just released but not yet reacquired by a
    queued acquirer momentarily reads as free. The dashboard polls every few
    seconds, so transient under-counts wash out and the gauge tracks real
    utilization accurately for monitoring purposes.

    Returns 0 if the semaphore has never been instantiated (no LLM call has
    run yet in this process), which is the correct "nothing in flight" value.
    """
    sem = _get_shared_semaphore()
    return max(0, GLOBAL_LLM_CONCURRENCY - sem._value) if sem else 0


def get_capacity() -> int:
    """The configured max concurrent LLM streams."""
    return GLOBAL_LLM_CONCURRENCY
