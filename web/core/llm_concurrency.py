"""Global LLM concurrency limiter (producer-consumer model).

All sub-agent LLM calls funnel through ``run_claude_query`` which acquires this
semaphore before dispatching to the provider.  This caps the number of
simultaneous in-flight LLM streams at ``GLOBAL_LLM_CONCURRENCY`` (default 2),
matching the GLM-5.2 Anthropic-compatible endpoint concurrency allowance.

The semaphore is FIFO (``asyncio.Semaphore`` is deque-backed) so no role is
starved: Master Scouts, Critics, Workers, Review, and all single-shot roles
queue fairly.  Master and Worker roles execute in different pipeline stages
(temporally separated by the linear stage machine), so they rarely compete for
permits simultaneously.

This replaces the former per-role ``_WORKER_SEMAPHORE`` adaptive backoff: that
mechanism only throttled Workers and left Master/Critic/final calls unbounded.
The global semaphore provides uniform coverage across all 17+ ``run_claude_query``
call sites.
"""

import asyncio
import os

GLOBAL_LLM_CONCURRENCY = int(os.environ.get("POK_GLOBAL_LLM_CONCURRENCY", "2"))

_GLOBAL_LLM_SEMAPHORE: "asyncio.Semaphore | None" = None


def get_global_llm_semaphore() -> asyncio.Semaphore:
    """Return the process-wide LLM dispatch semaphore.

    Lazily created on first call inside a running event loop (matching the
    ``_get_worker_semaphore`` pattern) to avoid binding to the wrong loop at
    import time.
    """
    global _GLOBAL_LLM_SEMAPHORE
    if _GLOBAL_LLM_SEMAPHORE is None:
        _GLOBAL_LLM_SEMAPHORE = asyncio.Semaphore(GLOBAL_LLM_CONCURRENCY)
    return _GLOBAL_LLM_SEMAPHORE
