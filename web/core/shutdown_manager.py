"""Asyncio-native shutdown coordinator for the evolution system.

Uses loop.add_signal_handler() (not signal.signal()) to correctly handle
SIGINT/SIGTERM within the asyncio event loop. Provides a clean shutdown_event
that all phases check between operations.

Usage:
    mgr = ShutdownManager()
    loop = asyncio.get_running_loop()
    mgr.install_signal_handlers(loop)

    while not mgr.is_shutting_down:
        await do_work()
"""

import asyncio
import signal


class ShutdownManager:
    def __init__(self, grace_period: float = 15.0):
        self._event = asyncio.Event()
        self._grace_period = grace_period
        self._callbacks: list[tuple] = []  # (coro_fn, name)

    @property
    def is_shutting_down(self) -> bool:
        return self._event.is_set()

    def request_shutdown(self):
        """Programmatically trigger shutdown (e.g. from web UI stop button)."""
        if not self._event.is_set():
            self._event.set()

    def register_cleanup(self, callback, name: str = ""):
        self._callbacks.append((callback, name))

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop):
        """Install SIGINT/SIGTERM handlers on the event loop.

        Must be called from within a running event loop.
        """
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._on_signal, sig)

    def _on_signal(self, sig):
        if self._event.is_set():
            return  # Second signal — let it raise naturally
        print(f"\n[Shutdown] Received {sig.name}, initiating graceful shutdown...")
        self._event.set()

    async def perform_cleanup(self):
        """Run registered cleanup callbacks with grace period timeout."""
        tasks = []
        for cb, name in self._callbacks:
            if asyncio.iscoroutinefunction(cb):
                tasks.append(asyncio.create_task(cb(), name=f"cleanup-{name}"))
            else:
                try:
                    cb()
                except Exception:
                    pass
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=self._grace_period)
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    async def wait_for_shutdown(self):
        await self._event.wait()
