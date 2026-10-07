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

FIFO ordering prevents starvation: no role is permanently blocked, and the
gate-stage roles (review/critic) compete on equal footing with producer roles.
The gate stages make far fewer LLM calls than Master/Workers, so starvation
is not a practical risk.

The limiter is a :class:`CrossLoopSemaphore`, not ``asyncio.Semaphore``.
Deterministic-route handlers (including ``run_crossover``) run on a private
event loop via ``run_async_off_event_loop``, while the saturator stays on the
ASGI loop.  CPython 3.12 ``asyncio.Semaphore`` only binds its loop on the
*first contended wait*; uncontended acquires do not bind, so a later waiter
on the private loop permanently binds the object and every ASGI acquire then
raises ``bound to a different event loop`` (v298: saturator sessions 13+
failed that way and occupancy stayed dark).  The native-precommit limiter
already uses a loop-agnostic primitive for the same reason.

The legacy partitioned getters (``get_consumer_llm_semaphore`` /
``get_producer_llm_semaphore``) are retained as backwards-compat aliases that
all return the same shared semaphore, so existing imports keep resolving.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

# Total concurrent in-flight LLM streams across ALL roles.
GLOBAL_LLM_CONCURRENCY = int(os.environ.get("POK_GLOBAL_LLM_CONCURRENCY", "4"))

# Legacy env vars retained for backwards compat but no longer partition.
_CONSUMER_LLM_CONCURRENCY = max(1, GLOBAL_LLM_CONCURRENCY // 3)
PRODUCER_LLM_CONCURRENCY = max(1, GLOBAL_LLM_CONCURRENCY - _CONSUMER_LLM_CONCURRENCY)
# Kept as a module-level constant for any code that still reads it.
CONSUMER_LLM_CONCURRENCY = GLOBAL_LLM_CONCURRENCY

_SHARED_LLM_SEMAPHORE: "CrossLoopSemaphore | None" = None
# Legacy single-pool alias (same object).
_GLOBAL_LLM_SEMAPHORE: "CrossLoopSemaphore | None" = None


@dataclass
class _Waiter:
    loop: asyncio.AbstractEventLoop
    fut: asyncio.Future
    dropped: bool = field(default=False)


class CrossLoopSemaphore:
    """FIFO permit pool usable from any asyncio event loop or thread.

    Waiters park on a Future created on *their* running loop.  ``release``
    wakes the oldest waiter with ``call_soon_threadsafe``, so ASGI saturator
    tasks and private-loop pipeline roles share one cap without binding the
    object to a single loop.

    Since 2026-10-07 the accounting is explicit ``_capacity``/``_holders``
    with ``_permits`` a derived value ``max(0, _capacity - _holders)``, so
    the AIMD controller can resize the live pool (:meth:`set_capacity`).
    A handoff to a parked waiter is granted only while ``_holders <
    _capacity`` — with resident waiters the pre-change release() transferred
    the freed permit unconditionally, so a saturated pool could never shrink.
    """

    def __init__(self, value: int) -> None:
        if int(value) < 0:
            raise ValueError("semaphore initial value must be >= 0")
        self._capacity = int(value)
        self._holders = 0
        self._waiters: deque[_Waiter] = deque()
        self._mutex = threading.RLock()

    @property
    def _permits(self) -> int:
        """Free permits — derived: never written directly."""
        with self._mutex:
            return max(0, self._capacity - self._holders)

    @property
    def _value(self) -> int:
        with self._mutex:
            return max(0, self._capacity - self._holders)

    def locked(self) -> bool:
        with self._mutex:
            return self._capacity - self._holders <= 0

    async def acquire(self) -> bool:
        loop = asyncio.get_running_loop()
        waiter: _Waiter | None = None
        with self._mutex:
            if self._holders < self._capacity:
                self._holders += 1
                return True
            fut: asyncio.Future = loop.create_future()
            waiter = _Waiter(loop=loop, fut=fut)
            self._waiters.append(waiter)
        assert waiter is not None
        try:
            await waiter.fut
            return True
        except asyncio.CancelledError:
            self._cancel_waiter(waiter)
            raise

    def release(self) -> None:
        with self._mutex:
            self._holders = max(0, self._holders - 1)
            self._wake_locked()

    def set_capacity(self, n: int) -> None:
        """Resize the pool. Growth wakes exactly ``delta`` waiters (each
        explicitly consuming one permit, so handoffs cannot drift); a shrink
        takes effect immediately — excess holders drain naturally and new
        acquires park until holders drop below the new capacity."""
        with self._mutex:
            new_capacity = int(n)
            if new_capacity < 0:
                raise ValueError("semaphore capacity must be >= 0")
            delta = new_capacity - self._capacity
            self._capacity = new_capacity
            if delta > 0:
                for _ in range(delta):
                    self._wake_locked()

    def adjust_capacity(self, delta: int) -> None:
        with self._mutex:
            self.set_capacity(self._capacity + int(delta))

    def _wake_locked(self) -> None:
        """Grant one free slot to the oldest live waiter, if capacity allows."""
        while self._waiters:
            if self._holders >= self._capacity:
                return
            waiter = self._waiters.popleft()
            if waiter.fut.done():
                continue
            try:
                waiter.loop.call_soon_threadsafe(self._deliver, waiter)
            except RuntimeError:
                waiter.dropped = True
                continue
            self._holders += 1
            return

    def _deliver(self, waiter: _Waiter) -> None:
        with self._mutex:
            if waiter.dropped or waiter.fut.done():
                # The scheduled grant is stranded (waiter cancelled in
                # flight): return the slot and re-offer it.
                self._holders = max(0, self._holders - 1)
                self._wake_locked()
                return
            waiter.fut.set_result(True)

    def _cancel_waiter(self, waiter: _Waiter) -> None:
        with self._mutex:
            try:
                self._waiters.remove(waiter)
                return
            except ValueError:
                pass
            if waiter.fut.done() and not waiter.fut.cancelled():
                # The grant was already delivered but the acquire coroutine
                # is dying with CancelledError: return the consumed slot.
                self._holders = max(0, self._holders - 1)
                self._wake_locked()
                return
            waiter.dropped = True

    async def __aenter__(self) -> "CrossLoopSemaphore":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False


# --- AIMD dynamic capacity (2026-10-07) --------------------------------------
#
# The static env ceiling (POK_GLOBAL_LLM_CONCURRENCY, 12 since commit
# 3e6068f7) is a floor to probe PAST, not the real limit: GLM's true 1302
# frequency wall floats across the day. The controller raises the live
# semaphore capacity additively while streams succeed and halves it when
# frequency-class failures (GLM 1302 / bare 429) cluster — >=
# AIMD_FAILURE_THRESHOLD inside the AIMD_WINDOW_SEC sliding window; isolated
# failures are tolerated. GLM 1308 quota exhaustion is a five-hour full stop
# unrelated to concurrency and is filtered at the reporting hook
# (llm_query_retry), never here. The learned level persists so a restart
# resumes nearby, and guards never trust it: the saturator's
# children/MemAvailable/cgroup-headroom gates re-read live machine state at
# every launch.

AIMD_MIN_LIMIT = 2
AIMD_MAX_LIMIT = 32
AIMD_WINDOW_SEC = 300.0
AIMD_FAILURE_THRESHOLD = 3
# Cautious probe pace above the static baseline (where 1302 storms live) and
# a fast recovery pace below it: a storm's multiplicative decrease lands the
# limit far under the static value, and the slow probe pace made the dip
# last ~50 minutes against a provider that historically sustains the static
# level. Operator direction 2026-10-07: climb back faster.
AIMD_RAISE_INTERVAL_SEC = 300.0
AIMD_FAST_RAISE_INTERVAL_SEC = 90.0
AIMD_RAISE_MIN_SUCCESSES = 4

_AIMD_LOCK = threading.Lock()
#: Current dynamic limit; None until first recovered from the state file.
_AIMD_LIMIT: "int | None" = None
#: Injectable state-file override (tests). None -> <module dir>/results/.
_AIMD_STATE_FILE: "Path | None" = None
_AIMD_FAILURE_TS: "list[float]" = []
_AIMD_SUCCESSES = 0
#: Last up/down limit change; the raise probe keeps a full interval's
#: distance from ANY change so a downshift gets provider breathing room.
_AIMD_LAST_LIMIT_CHANGE_TS: "float | None" = None

_log = logging.getLogger(__name__)


def _aimd_state_path() -> Path:
    if _AIMD_STATE_FILE is not None:
        return Path(_AIMD_STATE_FILE)
    # Same directory rate_limiter.py uses (its own local RESULTS_DIR) —
    # computed locally to avoid any import-order coupling.
    return Path(__file__).resolve().parent / "results" / "llm_aimd_state.json"


def _aimd_save(limit: int) -> None:
    """Persist the learned level (atomic write, best-effort)."""
    try:
        path = _aimd_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, json.dumps({"limit": int(limit)}).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(path))
    except OSError as exc:
        _log.warning("failed to persist LLM AIMD state: %s", exc)


def _aimd_load() -> int:
    """One-time lazy recovery of the persisted level (fail-open to static)."""
    global _AIMD_LIMIT
    if _AIMD_LIMIT is not None:
        return _AIMD_LIMIT
    limit = GLOBAL_LLM_CONCURRENCY
    try:
        path = _aimd_state_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                recovered = data.get("limit")
                if isinstance(recovered, int):
                    limit = max(AIMD_MIN_LIMIT, min(AIMD_MAX_LIMIT, recovered))
    except (OSError, ValueError, TypeError):
        limit = GLOBAL_LLM_CONCURRENCY
    _AIMD_LIMIT = limit
    return limit


def _aimd_apply(limit: int) -> None:
    """Record, persist, and push a new limit onto the live semaphore."""
    global _AIMD_LIMIT
    limit = max(AIMD_MIN_LIMIT, min(AIMD_MAX_LIMIT, int(limit)))
    previous = _AIMD_LIMIT if _AIMD_LIMIT is not None else limit
    _AIMD_LIMIT = limit
    _aimd_save(limit)
    if _SHARED_LLM_SEMAPHORE is not None:
        try:
            _SHARED_LLM_SEMAPHORE.set_capacity(limit)
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("failed to resize live LLM semaphore: %s", exc)
    if limit != previous:
        _log.info("LLM AIMD dynamic concurrency limit %d -> %d", previous, limit)
        try:
            import event_bus

            event_bus.emit(
                "pipeline.llm_aimd_limit_changed",
                "info",
                f"LLM 动态并发 {previous} -> {limit}",
                previous_limit=previous, limit=limit,
            )
        except Exception:
            pass


def get_aimd_limit() -> int:
    """Current dynamic LLM concurrency limit (recovered lazily, never None)."""
    with _AIMD_LOCK:
        return _aimd_load()


def note_llm_rate_limit_failure(now: "float | None" = None) -> None:
    """Report one frequency-class provider failure (GLM 1302 / bare 429).

    >= AIMD_FAILURE_THRESHOLD inside the sliding window -> multiplicative
    downshift (limit //= 2, floored at AIMD_MIN_LIMIT) and the window
    restarts. Isolated failures never downshift. 1308 quota bodies must be
    filtered by the caller — they indicate a usage cap, not congestion.
    """
    global _AIMD_FAILURE_TS, _AIMD_SUCCESSES, _AIMD_LAST_LIMIT_CHANGE_TS
    ts = time.time() if now is None else float(now)
    with _AIMD_LOCK:
        limit = _aimd_load()
        _AIMD_FAILURE_TS.append(ts)
        _AIMD_FAILURE_TS = [t for t in _AIMD_FAILURE_TS if t >= ts - AIMD_WINDOW_SEC]
        if len(_AIMD_FAILURE_TS) < AIMD_FAILURE_THRESHOLD:
            return
        _AIMD_FAILURE_TS = []
        _AIMD_SUCCESSES = 0
        _AIMD_LAST_LIMIT_CHANGE_TS = ts
        _aimd_apply(max(AIMD_MIN_LIMIT, limit // 2))


def note_llm_stream_success(now: "float | None" = None) -> None:
    """Report one completed provider stream.

    Additive +1 probe when the window is clean, >= AIMD_RAISE_MIN_SUCCESSES
    successes accumulated, and long enough since the last limit change:
    AIMD_FAST_RAISE_INTERVAL_SEC below the static baseline (storm recovery —
    get back to the sustained level quickly) and AIMD_RAISE_INTERVAL_SEC at or
    above it (cautious exploration where 1302 storms live). There is no
    static ceiling on the probe: upshifts are bounded here only by
    AIMD_MAX_LIMIT — the launch guards (live children count / MemAvailable /
    cgroup headroom) bound the ACTUAL load.
    """
    global _AIMD_SUCCESSES, _AIMD_LAST_LIMIT_CHANGE_TS
    ts = time.time() if now is None else float(now)
    with _AIMD_LOCK:
        limit = _aimd_load()
        _AIMD_FAILURE_TS[:] = [t for t in _AIMD_FAILURE_TS if t >= ts - AIMD_WINDOW_SEC]
        _AIMD_SUCCESSES += 1
        since_change = (
            ts - _AIMD_LAST_LIMIT_CHANGE_TS
            if _AIMD_LAST_LIMIT_CHANGE_TS is not None
            else float("inf")
        )
        # Fast recovery below the static baseline, cautious probe above it.
        interval = (
            AIMD_FAST_RAISE_INTERVAL_SEC
            if limit < GLOBAL_LLM_CONCURRENCY
            else AIMD_RAISE_INTERVAL_SEC
        )
        if (
            since_change < interval
            or _AIMD_FAILURE_TS
            or _AIMD_SUCCESSES < AIMD_RAISE_MIN_SUCCESSES
        ):
            return
        _AIMD_SUCCESSES = 0
        _AIMD_LAST_LIMIT_CHANGE_TS = ts
        _aimd_apply(min(AIMD_MAX_LIMIT, limit + 1))


def _get_shared_semaphore() -> CrossLoopSemaphore:
    global _SHARED_LLM_SEMAPHORE, _GLOBAL_LLM_SEMAPHORE
    if _SHARED_LLM_SEMAPHORE is None:
        # Materialize at the AIMD-recovered dynamic limit (fail-open to the
        # static env ceiling when no state file exists).
        _SHARED_LLM_SEMAPHORE = CrossLoopSemaphore(get_aimd_limit())
        _GLOBAL_LLM_SEMAPHORE = _SHARED_LLM_SEMAPHORE
    return _SHARED_LLM_SEMAPHORE


def get_global_llm_semaphore() -> CrossLoopSemaphore:
    """Return the single shared LLM dispatch semaphore."""
    return _get_shared_semaphore()


def get_consumer_llm_semaphore() -> CrossLoopSemaphore:
    """Legacy alias — returns the shared semaphore (no longer partitioned)."""
    return _get_shared_semaphore()


def get_producer_llm_semaphore() -> CrossLoopSemaphore:
    """Legacy alias — returns the shared semaphore (no longer partitioned)."""
    return _get_shared_semaphore()


def get_llm_semaphore_for_role(
    role_name: str | None,
) -> "CrossLoopSemaphore | _PipelinePrioritySemaphore":
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
    return max(0, get_capacity() - sem._value) if sem else 0


def get_capacity() -> int:
    """The current max concurrent LLM streams.

    Dynamic since 2026-10-07: the AIMD controller's live limit (persisted
    across restarts, fail-open to the static env ceiling). The static
    ``GLOBAL_LLM_CONCURRENCY`` remains the bootstrap default and the
    saturator's pipeline-reserve reference.
    """
    return get_aimd_limit()


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
# launched session then HOLDS its permit for the packet duration. A pipeline
# role that dispatches while all permits are held by saturator sessions queues
# behind them while its own dispatch timeout keeps running (v187, 2026-08-16:
# two consecutive 1800s worker timeouts whose entire budget was consumed by
# semaphore queue-wait behind three saturator sessions). Background fill
# must therefore be preemptable BY pipeline demand:
#   * pipeline roles acquire through _PipelinePrioritySemaphore, which
#     counts queue-pending demand;
#   * the saturator cancels its youngest in-flight session when the pool is
#     full and demand persists.

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

    def __init__(self, sem: "CrossLoopSemaphore | asyncio.Semaphore") -> None:
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
