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
import concurrent.futures
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
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix=thread_name_prefix,
    )
    loop = asyncio.get_running_loop()
    # Bridge the concurrent.futures result back to the event loop with a single
    # wakeup instead of a tight poll. ``add_done_callback`` schedules exactly
    # one ``loop.call_soon_threadsafe`` when the worker finishes, so the loop
    # sleeps idle (rather than waking 1000x/sec) while a slow blocking call
    # runs -- this was the root cause of /start starving the event loop while
    # ``_runtime_launch_barrier_snapshot`` ran for ~44s twice. The bridge does
    # not depend on the loop's default executor (satisfies the no-default-
    # executor contract) and remains responsive under constrained runtimes.
    aio_future: asyncio.Future = loop.create_future()

    def _transfer_result(done_future: concurrent.futures.Future) -> None:
        def _set_result() -> None:
            if not aio_future.done():
                try:
                    aio_future.set_result(done_future.result())
                except BaseException as exc:  # noqa: BLE001 - propagate any
                    aio_future.set_exception(exc)
        loop.call_soon_threadsafe(_set_result)

    try:
        work_future = executor.submit(context.run, invocation)
    except BaseException:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    work_future.add_done_callback(_transfer_result)
    try:
        return await aio_future
    finally:
        if work_future.done():
            executor.shutdown(wait=True, cancel_futures=False)
        else:
            work_future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
