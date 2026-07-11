"""Explicit async boundary for blocking infrastructure calls.

Official EXE orchestration performs file locking, process inspection, and long
blocking waits. It must not consume the event loop's shared default executor:
that couples certification latency to rating/evolution work and makes executor
shutdown part of the Web server lifecycle. Each invocation therefore owns one
short-lived worker and deterministically releases it when the call completes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import contextvars
from functools import partial
from typing import Any, TypeVar


ResultT = TypeVar("ResultT")


async def run_blocking_isolated(
    function: Callable[..., ResultT],
    /,
    *args: Any,
    thread_name_prefix: str = "pok-blocking",
    **kwargs: Any,
) -> ResultT:
    """Run one blocking call outside the shared asyncio executor.

    Context variables are propagated like ``asyncio.to_thread``. Cancellation
    cannot stop an already-running Python thread, so cancellation releases the
    executor without waiting; the owned worker exits after the blocking call.
    """

    context = contextvars.copy_context()
    invocation = partial(function, *args, **kwargs)
    executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix=thread_name_prefix,
    )
    try:
        concurrent = executor.submit(context.run, invocation)
    except BaseException:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    try:
        # Poll the owned future instead of relying solely on the loop's
        # cross-thread notification pipe. This keeps the boundary responsive
        # during interpreter/server shutdown and under constrained runtimes.
        while not concurrent.done():
            await asyncio.sleep(0.001)
        return concurrent.result()
    finally:
        if concurrent.done():
            executor.shutdown(wait=True, cancel_futures=False)
        else:
            concurrent.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
