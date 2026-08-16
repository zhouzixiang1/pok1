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
import time

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

    Pipeline roles get the :class:`_PipelinePrioritySemaphore` wrapper (same
    FIFO semaphore underneath) so their queue-wait is visible to the
    background-fill preemption logic; background fill roles (SATURATOR) get
    the raw semaphore — they are the preemptable class, never the preempting
    one.
    """
    sem = _get_shared_semaphore()
    if role_name and "SATURATOR" in str(role_name).upper():
        return sem
    return _PipelinePrioritySemaphore(sem)


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


def llm_semaphore_has_capacity(n: int = 1) -> bool:
    """Advisory predicate: are at least ``n`` LLM permits likely free right now?

    Used by the deep-parallelism producer to decide whether to launch another
    draft (or a filler draft) so the pool is kept saturated without
    over-launching. Reads ``semaphore._value`` — the same instantaneous read
    ``get_active_stream_count`` uses — so it is a *hint*, not a reservation:
    a permit that was just released but not yet reacquired by a queued acquirer
    momentarily reads free, and the launched draft's first LLM call
    simply queues on the semaphore if the hint was optimistic (FIFO fairness
    is preserved). This is the desired behaviour: we *want* to keep a draft
    staged behind the semaphore so it starts the moment a permit frees, rather
    than waiting for a poll interval to notice capacity.

    Returns ``False`` when the semaphore has never been instantiated (treated
    as "unknown capacity — do not launch speculatively"); the first real LLM
    call materializes it.
    """
    sem = _get_shared_semaphore()
    return bool(sem and sem._value >= n)


# --- Pipeline preemption over background fill work -------------------------
#
# The saturator keeps free permits filled, which is its purpose — but a
# launched session then HOLDS its permit for ~25 min. A pipeline role that
# dispatches while all permits are held by saturator sessions queues behind
# them while its own dispatch timeout keeps running (v187, 2026-08-16:
# two consecutive 1800s worker timeouts whose entire budget was consumed by
# semaphore queue-wait behind three saturator sessions). Background fill
# must therefore be preemptable BY pipeline demand:
#   * pipeline roles acquire through _PipelinePrioritySemaphore, which
#     counts queue-pending demand;
#   * the saturator holds new launches while pipeline roles are pending and
#     cancels its youngest in-flight session when demand persists.

_pipeline_pending: int = 0
_pipeline_first_pending_ts: "float | None" = None


def _note_pipeline_pending(delta: int) -> None:
    global _pipeline_pending, _pipeline_first_pending_ts
    _pipeline_pending = max(0, _pipeline_pending + delta)
    if _pipeline_pending > 0 and _pipeline_first_pending_ts is None:
        _pipeline_first_pending_ts = time.time()
    elif _pipeline_pending == 0:
        _pipeline_first_pending_ts = None


def pipeline_pending_count() -> int:
    """Pipeline LLM roles currently queued waiting for a permit."""
    return _pipeline_pending


def pipeline_pending_age_sec() -> float:
    """Seconds since the oldest continuous pipeline queue-demand began.

    ``0.0`` when no pipeline role is waiting. Preemption consumers treat a
    sustained nonzero age (e.g. >30s) as the signal that held background
    permits, not queue churn, are blocking the pipeline."""
    if _pipeline_first_pending_ts is None:
        return 0.0
    return time.time() - _pipeline_first_pending_ts


class _PipelinePrioritySemaphore:
    """async-with semaphore wrapper that counts queue-pending pipeline demand.

    Delegates to the shared FIFO semaphore; the ONLY behavioral addition is
    the pending counter around the acquire, scoped exactly to the wait (the
    retry loop acquires per attempt, so backoff sleeps between attempts do
    not count as demand)."""

    def __init__(self, sem: asyncio.Semaphore) -> None:
        self._sem = sem

    async def __aenter__(self) -> "_PipelinePrioritySemaphore":
        _note_pipeline_pending(1)
        try:
            await self._sem.acquire()
        finally:
            _note_pipeline_pending(-1)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._sem.release()
        return False

    async def acquire(self) -> "_PipelinePrioritySemaphore":
        return await self.__aenter__()

    def release(self) -> None:
        self._sem.release()

    @property
    def _value(self) -> int:
        return self._sem._value
