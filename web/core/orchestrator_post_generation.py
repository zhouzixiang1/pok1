"""Post-generation cleanup coroutine -- generation wrap-up housekeeping.

This module hosts the single coroutine that wraps
``generation_scheduler.post_generation_cleanup`` in a timeout + structured
logging envelope so that one slow wrap-up cannot block the next evolution
cycle indefinitely.

Members moved here:

* ``POST_GENERATION_CLEANUP_TIMEOUT``  -- default wall-clock budget (seconds).
* ``_run_post_generation_cleanup_with_timeout``  -- the timed coroutine.

IMPORTANT -- shared-symbol access model
---------------------------------------
Symbols referenced by this body that live in ``orchestrator`` (``log``,
``log_system_event``, ``POST_GENERATION_CLEANUP_TIMEOUT``) are written as
``_o.<name>`` so they resolve against the live ``orchestrator`` module
attribute, matching the pattern proven by ``orchestrator_branch_guard`` /
``tool_commit_archivist``.  This lets the test suite's
``monkeypatch.setattr(orchestrator, "_run_post_generation_cleanup_with_timeout",
...)`` (which replaces the *re-exported* attribute on ``orchestrator``)
continue to drive ``orchestrator_loop``'s bare-global call site.
"""

from __future__ import annotations

import asyncio
import os
import time

import orchestrator as _o
from orchestrator_cost_policy import OperatorGenerationCostLimitExceeded


# Wall-clock budget for post-generation housekeeping (archivist / consolidation
# / scheduler bookkeeping).  Read once at import time; the coroutine reads the
# live ``_o.POST_GENERATION_CLEANUP_TIMEOUT`` so tests / operators can mutate
# the orchestrator attribute at runtime if needed.
POST_GENERATION_CLEANUP_TIMEOUT = int(os.environ.get("POK_POST_GENERATION_CLEANUP_TIMEOUT", "900"))


async def _run_post_generation_cleanup_with_timeout(shutdown_mgr, ui, gen_ctx, gen_count=None):
    """Run post-generation housekeeping without letting it block evolution forever."""
    from generation_scheduler import post_generation_cleanup

    version = getattr(gen_ctx, "next_v", None)
    source_v = getattr(gen_ctx, "source_v", None)
    started = time.time()
    _o.log_system_event(
        "orchestrator.post_cleanup_start",
        "info",
        f"Post-generation cleanup starting for v{version}",
        {
            "version": version,
            "source_v": source_v,
            "gen_count": gen_count,
            "timeout_s": _o.POST_GENERATION_CLEANUP_TIMEOUT,
        },
    )
    try:
        await asyncio.wait_for(
            post_generation_cleanup(shutdown_mgr, ui, gen_ctx),
            timeout=_o.POST_GENERATION_CLEANUP_TIMEOUT,
        )
    except asyncio.TimeoutError:
        elapsed = time.time() - started
        msg = (
            f"Post-generation cleanup timed out for v{version} after "
            f"{_o.POST_GENERATION_CLEANUP_TIMEOUT}s; stopping before successor "
            "scheduling because the checkpoint-free boundary remains blocked."
        )
        _o.log.warning(msg)
        if ui:
            ui.log_history(msg, "warn")
        _o.log_system_event(
            "orchestrator.post_cleanup_timeout",
            "warn",
            msg,
            {
                "version": version,
                "source_v": source_v,
                "gen_count": gen_count,
                "elapsed_sec": round(elapsed, 2),
                "timeout_s": _o.POST_GENERATION_CLEANUP_TIMEOUT,
            },
        )
        return False
    except OperatorGenerationCostLimitExceeded:
        # Archivist/consolidation calls are part of the same generation.  Do not
        # translate an operator stop into best-effort cleanup and then start a
        # fresh generation with a reset scope.
        raise
    except Exception as e:
        elapsed = time.time() - started
        msg = f"Post-generation cleanup failed for v{version}: {str(e)[:180]}"
        _o.log.exception(msg)
        if ui:
            ui.log_history(msg, "warn")
        _o.log_system_event(
            "orchestrator.post_cleanup_failed",
            "error",
            msg,
            {
                "version": version,
                "source_v": source_v,
                "gen_count": gen_count,
                "elapsed_sec": round(elapsed, 2),
                "error": str(e)[:500],
            },
        )
        return False

    elapsed = time.time() - started
    _o.log_system_event(
        "orchestrator.post_cleanup_done",
        "info",
        f"Post-generation cleanup finished for v{version} in {elapsed:.1f}s",
        {
            "version": version,
            "source_v": source_v,
            "gen_count": gen_count,
            "elapsed_sec": round(elapsed, 2),
        },
    )
    return True
