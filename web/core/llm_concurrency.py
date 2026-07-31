"""Global LLM concurrency limiter (producer-consumer model).

All sub-agent LLM calls funnel through ``run_claude_query`` which acquires a
semaphore before dispatching to the provider.  This caps the number of
simultaneous in-flight LLM streams.

**Partitioned model (multi-ahead).** The single global semaphore is split into
two sub-pools so the consumer lane (gate stages: review, critic — the
publication critical path) is never starved by the producer lane (Master
Scouts/Critics/final, Workers, direction audit — the draft-preparation path):

- ``consumer`` sub-pool: ``POK_LLM_CONSUMER_CONCURRENCY`` permits (default
  ``max(1, total // 3)``).  Used by gate/review/critic roles.
- ``producer`` sub-pool: the remainder (``total - consumer``).  Used by
  Master/Worker/direction roles.

When ``POK_GLOBAL_LLM_CONCURRENCY`` is unset (the legacy default of 2), the
partitioning is transparent: ``total=2`` → consumer=1, producer=1, and the
effective behavior matches the former single ``Semaphore(2)`` (each lane gets
its own 1-permit pool, but since lanes are temporally separated without
multi-ahead, contention is identical).  When raised to 3+ (recommended for
multi-ahead), the consumer is guaranteed an exclusive permit.

Both sub-pools are FIFO (``asyncio.Semaphore`` is deque-backed) so no role
within a lane is starved.
"""

import asyncio
import os

GLOBAL_LLM_CONCURRENCY = int(os.environ.get("POK_GLOBAL_LLM_CONCURRENCY", "2"))

# Consumer lane (gate stages: review, critic — publication critical path).
# Default: at least 1 permit, roughly 1/3 of the total pool.
_POK_CONSUMER_CONCURRENCY_ENV = os.environ.get("POK_LLM_CONSUMER_CONCURRENCY")
if _POK_CONSUMER_CONCURRENCY_ENV is not None:
    CONSUMER_LLM_CONCURRENCY = max(1, int(_POK_CONSUMER_CONCURRENCY_ENV))
else:
    CONSUMER_LLM_CONCURRENCY = max(1, GLOBAL_LLM_CONCURRENCY // 3)
# Producer lane gets the remainder (at least 1).
PRODUCER_LLM_CONCURRENCY = max(1, GLOBAL_LLM_CONCURRENCY - CONSUMER_LLM_CONCURRENCY)

# Role → lane classification.  Consumer = gate stages (publication critical
# path); producer = planning/implementation stages (draft preparation).
_CONSUMER_ROLE_MARKERS = frozenset({
    "review",
    "reviewer",
    "critic",
})

_GLOBAL_LLM_SEMAPHORE: "asyncio.Semaphore | None" = None
_CONSUMER_LLM_SEMAPHORE: "asyncio.Semaphore | None" = None
_PRODUCER_LLM_SEMAPHORE: "asyncio.Semaphore | None" = None


def get_global_llm_semaphore() -> asyncio.Semaphore:
    """Return the process-wide LLM dispatch semaphore (legacy single-pool).

    Lazily created on first call inside a running event loop.  Kept for
    backwards compatibility; new code should use ``get_llm_semaphore_for_role``
    which dispatches to the partitioned sub-pools.
    """
    global _GLOBAL_LLM_SEMAPHORE
    if _GLOBAL_LLM_SEMAPHORE is None:
        _GLOBAL_LLM_SEMAPHORE = asyncio.Semaphore(GLOBAL_LLM_CONCURRENCY)
    return _GLOBAL_LLM_SEMAPHORE


def get_consumer_llm_semaphore() -> asyncio.Semaphore:
    """The consumer-lane sub-pool (gate/review/critic roles)."""
    global _CONSUMER_LLM_SEMAPHORE
    if _CONSUMER_LLM_SEMAPHORE is None:
        _CONSUMER_LLM_SEMAPHORE = asyncio.Semaphore(CONSUMER_LLM_CONCURRENCY)
    return _CONSUMER_LLM_SEMAPHORE


def get_producer_llm_semaphore() -> asyncio.Semaphore:
    """The producer-lane sub-pool (Master/Worker/direction roles)."""
    global _PRODUCER_LLM_SEMAPHORE
    if _PRODUCER_LLM_SEMAPHORE is None:
        _PRODUCER_LLM_SEMAPHORE = asyncio.Semaphore(PRODUCER_LLM_CONCURRENCY)
    return _PRODUCER_LLM_SEMAPHORE


def get_llm_semaphore_for_role(role_name: str | None) -> asyncio.Semaphore:
    """Return the appropriate sub-pool semaphore for ``role_name``.

    Consumer-lane roles (review/critic) get the consumer sub-pool so the
    publication critical path is never starved by producer Scout bursts.  All
    other roles (Master/Worker/direction/final) get the producer sub-pool.
    """
    if role_name and any(marker in str(role_name).lower() for marker in _CONSUMER_ROLE_MARKERS):
        return get_consumer_llm_semaphore()
    return get_producer_llm_semaphore()
