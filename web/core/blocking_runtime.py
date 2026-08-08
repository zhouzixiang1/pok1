"""Explicit async boundary for blocking infrastructure calls.

Official EXE orchestration performs file locking, process inspection, and long
blocking waits. It must not consume the event loop's shared default executor:
that couples certification latency to rating/evolution work and makes executor
shutdown part of the Web server lifecycle. Each invocation therefore owns one
short-lived worker and deterministically releases it when the call completes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
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


async def run_async_off_event_loop(
    coro_fn: Callable[..., Awaitable[ResultT]],
    /,
    *args: Any,
    thread_name_prefix: str = "pok-async-offloop",
    **kwargs: Any,
) -> ResultT:
    """Run an async coroutine off the calling event loop in an owned worker.

    This is the companion of :func:`run_blocking_isolated` for the case where
    the blocking work is itself an ``async`` coroutine (e.g. the canonical MCP
    gate handlers ``run_quality_gates``/``run_review``/``run_critic``/
    ``run_precommit_eval``). Those coroutines drive the native TCP match engine
    (``asyncio.start_server``/``asyncio.wait``/``loop.time``) and inline
    synchronous file-hashing/subprocess I/O (``bot_artifact.hash_path`` /
    ``artifact_manifest``). Running the coroutine inline on the orchestrator's
    ASGI event loop blocks HTTP for the full match duration (10+ minutes per
    precommit).

    A plain :func:`run_blocking_isolated` cannot be used directly because the
    value is a coroutine (it needs a running loop, and the worker thread starts
    with none). Instead this drives the coroutine with a *fresh, private* event
    loop created and torn down inside the worker thread via :func:`asyncio.run`,
    while reusing the same owned-worker / single-wakeup / context-propagating
    transport as :func:`run_blocking_isolated`. The native match's heartbeat
    sidecar (which reads the process-wide
    ``_NATIVE_MATCH_DISPATCH_NONCES`` set from the main loop) keeps working
    because the dispatch nonce is process-global and the bound ContextVar is
    propagated into the worker by :func:`contextvars.copy_context`.

    The orchestrator's ASGI event loop stays free to serve HTTP while the gate
    chain runs. Semantics, checkpoint writes, CAS identities, ContextVar scope
    (slot override / consumer-in-chain authority / native-match dispatch nonce)
    and the consumer lifecycle FSM transitions are unchanged -- only the loop
    the coroutine runs on changes.
    """

    def _run_in_private_loop() -> ResultT:
        return asyncio.run(coro_fn(*args, **kwargs))

    return await run_blocking_isolated(
        _run_in_private_loop,
        thread_name_prefix=thread_name_prefix,
    )
