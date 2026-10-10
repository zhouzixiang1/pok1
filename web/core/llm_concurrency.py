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

    @property
    def holders(self) -> int:
        """Real occupancy: permits currently held, even above capacity.

        During the drain after a shrink, ``_holders`` legitimately exceeds
        ``_capacity`` until the excess streams finish; the derived
        ``_value`` reads 0 there, so only this property observes the true
        in-flight count (F4, 2026-10-08).
        """
        with self._mutex:
            return self._holders

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
# semaphore capacity additively while streams succeed and subtracts
# ``max(2, limit // 4)`` (>= -25% or -2, whichever unloads more — F1,
# 2026-10-08; the former halving crashed the live wall 8-9 to 4 on every
# storm and the sawtooth cost -10.7% overnight throughput) when
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
# a fast recovery pace below it: a storm's decrease lands the
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
#: Persisted with the limit since 2026-10-08 (F3) so a restart cannot
#: bypass the cooldown once via since_change=inf.
_AIMD_LAST_LIMIT_CHANGE_TS: "float | None" = None
#: Monotonic apply sequence (F2, 2026-10-08): assigned in-lock, checked by
#: the out-of-lock persistence so a delayed older save can never overwrite
#: a newer limit/clock.
_AIMD_APPLY_SEQ = 0
#: Serializes the out-of-lock atomic state-file replaces (F2): two threads
#: must never interleave their tmp/replace pairs.
_AIMD_PERSIST_LOCK = threading.Lock()

_log = logging.getLogger(__name__)


def _aimd_state_path() -> Path:
    if _AIMD_STATE_FILE is not None:
        return Path(_AIMD_STATE_FILE)
    # Same directory rate_limiter.py uses (its own local RESULTS_DIR) —
    # computed locally to avoid any import-order coupling.
    return Path(__file__).resolve().parent / "results" / "llm_aimd_state.json"


def _aimd_save(limit: int, last_change_ts: "float | None" = None) -> None:
    """Persist the learned level and its change clock (atomic, best-effort)."""
    try:
        path = _aimd_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        payload = {"limit": int(limit)}
        if last_change_ts is not None:
            payload["last_change_ts"] = float(last_change_ts)
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, json.dumps(payload).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(path))
    except OSError as exc:
        _log.warning("failed to persist LLM AIMD state: %s", exc)


def _aimd_load() -> int:
    """One-time lazy recovery of the persisted level (fail-open to static).

    F3 (2026-10-08): the persisted ``last_change_ts`` is recovered with the
    limit so the raise-interval clock survives restarts. A missing clock
    (legacy state file) conservatively initializes to NOW — the process then
    waits one full interval before the first upshift probe instead of
    bypassing the cooldown once via since_change=inf.
    """
    global _AIMD_LIMIT, _AIMD_LAST_LIMIT_CHANGE_TS
    if _AIMD_LIMIT is not None:
        return _AIMD_LIMIT
    limit = GLOBAL_LLM_CONCURRENCY
    recovered_change_ts: "float | None" = None
    try:
        path = _aimd_state_path()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                recovered = data.get("limit")
                if isinstance(recovered, int):
                    limit = max(AIMD_MIN_LIMIT, min(AIMD_MAX_LIMIT, recovered))
                ts = data.get("last_change_ts")
                if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                    recovered_change_ts = float(ts)
    except (OSError, ValueError, TypeError):
        limit = GLOBAL_LLM_CONCURRENCY
    if _AIMD_LAST_LIMIT_CHANGE_TS is None:
        _AIMD_LAST_LIMIT_CHANGE_TS = (
            time.time() if recovered_change_ts is None else recovered_change_ts
        )
    _AIMD_LIMIT = limit
    return limit


def _aimd_apply_locked(
    limit: int, last_change_ts: "float | None" = None
) -> "tuple[int, int, int, float | None]":
    """In-lock half of an apply (caller holds ``_AIMD_LOCK``): pure memory.

    Flips the module limit, stamps the monotonic apply sequence, and resizes
    the live semaphore. Returns the deferred out-of-lock work as
    ``(seq, previous, limit, last_change_ts)`` for :func:`_aimd_finish_apply`.
    """
    global _AIMD_LIMIT, _AIMD_APPLY_SEQ
    limit = max(AIMD_MIN_LIMIT, min(AIMD_MAX_LIMIT, int(limit)))
    previous = _AIMD_LIMIT if _AIMD_LIMIT is not None else limit
    _AIMD_LIMIT = limit
    _AIMD_APPLY_SEQ += 1
    seq = _AIMD_APPLY_SEQ
    if _SHARED_LLM_SEMAPHORE is not None:
        try:
            _SHARED_LLM_SEMAPHORE.set_capacity(limit)
        except Exception as exc:  # pragma: no cover - defensive
            _log.warning("failed to resize live LLM semaphore: %s", exc)
    return seq, previous, limit, last_change_ts


def _aimd_finish_apply(
    seq: int, previous: int, limit: int, last_change_ts: "float | None"
) -> None:
    """Out-of-lock half of an apply: the state-file fsync + events.jsonl append.

    F2 (2026-10-08): these used to run under ``_AIMD_LOCK`` and blocked
    ``get_capacity()`` readers for the fsync + append duration (measured
    503-760ms in production). Two guards keep the deferred write correct:
      * the monotonic apply seq — the save/emit only lands while this apply
        is still the latest one, so an older delayed save can never
        overwrite a newer limit/clock (out-of-order overwrite);
      * ``_AIMD_PERSIST_LOCK`` serializes the atomic-replace dances so two
        threads never interleave their tmp/replace pairs.
    """
    with _AIMD_PERSIST_LOCK:
        with _AIMD_LOCK:
            stale = seq != _AIMD_APPLY_SEQ
        if stale:
            return
        _aimd_save(limit, last_change_ts)
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

    >= AIMD_FAILURE_THRESHOLD inside the sliding window triggers a downshift
    and the window CLEARS — that clearing is deliberate design (kept
    2026-10-08, F5): the storm that TRIGGERED the downshift leaves no
    residue blocking later probes, so post-storm pacing comes from the
    ``_AIMD_LAST_LIMIT_CHANGE_TS`` cooldown plus freshly accumulated
    successes, NOT from the old window. (The fc9edcc8 commit message's claim
    that "the 300s failure window still blocks all probes right after a
    storm burst" was inaccurate.) Isolated failures never downshift. 1308
    quota bodies must be filtered by the caller — they indicate a usage cap,
    not congestion.

    Downshift step (F1, 2026-10-08): ``limit - max(2, limit // 4)`` — unload
    at least 25% or 2 permits, whichever is MORE, floored at AIMD_MIN_LIMIT.
    The former ``limit // 2`` halving crashed the live wall (8-9) to 4 on
    every storm; the softened step keeps low tiers shallow (-2, fast to
    climb back) while high tiers still unload substantially (32 -> 24).
    """
    global _AIMD_FAILURE_TS, _AIMD_SUCCESSES, _AIMD_LAST_LIMIT_CHANGE_TS
    ts = time.time() if now is None else float(now)
    deferred = None
    with _AIMD_LOCK:
        limit = _aimd_load()
        _AIMD_FAILURE_TS.append(ts)
        _AIMD_FAILURE_TS = [t for t in _AIMD_FAILURE_TS if t >= ts - AIMD_WINDOW_SEC]
        if len(_AIMD_FAILURE_TS) < AIMD_FAILURE_THRESHOLD:
            return
        _AIMD_FAILURE_TS = []
        _AIMD_SUCCESSES = 0
        _AIMD_LAST_LIMIT_CHANGE_TS = ts
        deferred = _aimd_apply_locked(
            max(AIMD_MIN_LIMIT, limit - max(2, limit // 4)), last_change_ts=ts
        )
    if deferred is not None:
        _aimd_finish_apply(*deferred)


def note_llm_stream_success(now: "float | None" = None) -> None:
    """Report one completed provider stream.

    Additive +1 probe when the window is clean, >= AIMD_RAISE_MIN_SUCCESSES
    successes accumulated, and long enough since the last limit change:
    AIMD_FAST_RAISE_INTERVAL_SEC below the static baseline (storm recovery —
    get back to the sustained level quickly) and AIMD_RAISE_INTERVAL_SEC at or
    above it (cautious exploration where 1302 storms live). "Clean window"
    means no NEW failures since the last downshift CLEARED it — the window
    CLEARS on the downshift it triggered (see note_llm_rate_limit_failure),
    so the triggering storm's own residue never blocks the recovery probes.
    There is no static ceiling on the probe: upshifts are bounded here only
    by AIMD_MAX_LIMIT — the launch guards (live children count / MemAvailable
    / cgroup headroom) bound the ACTUAL load.
    """
    global _AIMD_SUCCESSES, _AIMD_LAST_LIMIT_CHANGE_TS
    ts = time.time() if now is None else float(now)
    deferred = None
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
        if not (
            since_change < interval
            or _AIMD_FAILURE_TS
            or _AIMD_SUCCESSES < AIMD_RAISE_MIN_SUCCESSES
        ):
            _AIMD_SUCCESSES = 0
            _AIMD_LAST_LIMIT_CHANGE_TS = ts
            deferred = _aimd_apply_locked(
                min(AIMD_MAX_LIMIT, limit + 1), last_change_ts=ts
            )
    if deferred is not None:
        _aimd_finish_apply(*deferred)


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
    """Count of currently in-use LLM permits — the semaphore's real holders.

    F4 (2026-10-08): this used to read ``max(0, capacity - _value)`` which
    equals ``min(capacity, holders)`` and UNDER-COUNTED the drain period
    after a downshift — with the capacity softened to 2 while 32 streams
    were still in flight, the gauge showed 2. Reading the real holders
    observes the true occupancy; the drain-complete predicate
    (``active == 0``) is unchanged because capacity never drops below
    AIMD_MIN_LIMIT (2), so ``min(capacity, holders) == 0`` exactly when
    ``holders == 0``.

    Returns 0 if the semaphore has never been instantiated (no LLM call has
    run yet in this process), which is the correct "nothing in flight" value.
    """
    sem = _get_shared_semaphore()
    return sem.holders if sem else 0


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


# --- 5h rolling-window quota pacing (2026-10-10, w2/w4/w5 root cause) -------
#
# GLM's coding-plan 1308 error caps usage over a rolling ~5h window
# (~178M tokens of observed window capacity). Unpaced dispatch burned
# 16-40M tok/h (avg ~34M/h) — the saturator kept every permit filled — so
# the window front-loaded, punched through the provider cap, and the
# rate_limiter + durable quota_429 pause then LEGITIMATELY parked all
# dispatch for the window tail (observed: whole zero-token hours at
# 18:42-19:55, 14-16:00, 00:24-01:57). The fix is pacing, not more
# retry: clamp the mean OPPORTUNISTIC dispatch rate (the saturator lane)
# to POK_LLM_QUOTA_WINDOW_BUDGET_TOKENS / POK_LLM_QUOTA_WINDOW_SEC. With
# the committed production values (150M / 18000s = 30M tok/h) the pace
# still clears the 20M/h hourly KPI while leaving ~16% headroom under the
# observed cap, so the window tail no longer collapses to zero.
#
# Multi-scale rolling check: the trailing-1h spend must stay under
# budget/5 (kills the "front-load 34M/h then starve" sawtooth) and the
# trailing-window spend under the full budget (belt-and-braces, and the
# binding scale for burn seeded from history older than an hour). The
# accounting basis is llm_call_metrics.jsonl ``total_tokens``
# (input+output) — the same basis the incident windows were measured on.
# Pipeline roles (Master/Workers/gates) are NEVER gated here: they are the
# product work; if their burn alone exceeds the budget, that is a
# capacity decision, not something to silently starve.

_QUOTA_PACER_DEFAULT_WINDOW_SEC = 18000.0

#: Wall-clock stamp of this process's module load. The seed dedupes against
#: it, NOT against singleton-materialization time: the singleton is created
#: lazily (possibly after the first in-process call already appended its
#: metrics row), so a construction-time boot stamp would classify that row
#: as "pre-boot" and double-book the burn. Module load ≈ process start, and
#: every row this process writes lands after it — and because the hook
#: imports this module lazily, a first record written BEFORE the import
#: would land before this stamp, so the seed (file) and the hook (memory)
#: could both count it; the (call_id, attempt) dedupe below closes that
#: last seam exactly once either way.
_QUOTA_MODULE_LOAD_TS = time.time()


def _quota_window_budget_tokens() -> int:
    """Token budget for the rolling pace window; ``0`` disables the pacer."""

    raw = os.environ.get("POK_LLM_QUOTA_WINDOW_BUDGET_TOKENS", "0")
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return 0


def _quota_window_sec() -> float:
    raw = os.environ.get("POK_LLM_QUOTA_WINDOW_SEC", "")
    try:
        value = float(raw) if raw else _QUOTA_PACER_DEFAULT_WINDOW_SEC
    except (TypeError, ValueError):
        return _QUOTA_PACER_DEFAULT_WINDOW_SEC
    return max(600.0, value)


def _quota_cache_weight() -> float:
    """Weight of cache tokens in the pace line (2026-10-10 calibration).

    ``POK_LLM_QUOTA_CACHE_WEIGHT`` (default 0.0): cache_read_input_tokens +
    cache_creation_input_tokens are added to the weighted window spend at
    this weight. The DEFAULT caliber is deliberately wider than the
    historical one: the ledger books every completed provider attempt row
    that carries usage — successful AND failed (``success=False``) rows
    alike (the 2026-10-10 blind-spot fix; failure rows with real token burn
    previously escaped accounting and silently under-counted the window).
    At the default weight 0.0 those rows still enter as their plain
    input+output ``total_tokens`` only, with no cache contribution; the
    provider's true 5h usage unit is unknown, so the cache weight is raised
    only after the dual-caliber snapshots carried on
    ``pipeline.llm_quota_exceeded_detected`` events are calibrated against
    a real 1308.
    """
    raw = os.environ.get("POK_LLM_QUOTA_CACHE_WEIGHT", "")
    try:
        value = float(raw) if raw else 0.0
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, value)


class QuotaPacer:
    """Rolling-window token burn account with a mean-rate clamp.

    Records ``(ts, tokens)`` spend events (one per completed provider
    attempt, fed by llm_call_metrics.record_llm_call_metrics) and answers
    whether the OPPORTUNISTIC lane (saturator) may launch right now. The
    process-start seed replays the durable metrics tail so a restart does
    not reset the clamp mid-window — without it, a fresh process would
    happily re-burn the window the old process already spent.
    """

    #: Bounded event history (a 5h window holds ~150-500 calls; 4096 is a
    #: generous cap that still bounds memory on pathological churn).
    _MAX_EVENTS = 4096
    #: Seeding reads at most this many bytes/lines of the metrics tail.
    _SEED_MAX_BYTES = 8 << 20
    _SEED_MAX_LINES = 6000
    #: Seen-key set cap (coarse clear-on-overflow; see _register_call_key_locked).
    _MAX_CALL_KEYS = 2 * _MAX_EVENTS
    #: The short pace scale (trailing-1h mean-rate line).
    SHORT_SCALE_SEC = 3600.0

    def __init__(self, boot_ts: "float | None" = None) -> None:
        self._lock = threading.Lock()
        # (ts, base_tokens, cache_tokens): base is the historical
        # input+output-only total_tokens; cache is
        # cache_read_input_tokens + cache_creation_input_tokens. The gate
        # consumes base + weight*cache (weight default 0.0 == historical
        # behavior exactly); spend_in_window stays the base-only caliber.
        self._events: "deque[tuple[float, int, int]]" = deque()
        # Composite (call_id, attempt) keys of already-counted records, so a
        # row that is BOTH in the durable metrics tail AND notified by the
        # in-process hook is counted exactly once (the lazy-import first-row
        # seam). ``call_id`` alone is NOT unique per record — signature-retry
        # attempts reuse it (llm_query_retry.py creates one billing_call_id
        # per retry loop) — so the attempt number is part of the key.
        self._seen_call_keys: "set[tuple] | None" = set()
        # Default to the module-load stamp (see _QUOTA_MODULE_LOAD_TS): the
        # seed must exclude rows written by THIS process, whose hook events
        # arrive in memory instead.
        self._boot_ts = (
            _QUOTA_MODULE_LOAD_TS if boot_ts is None else float(boot_ts)
        )
        self._seeded = False

    # -- accounting ---------------------------------------------------------

    def note_usage(
        self,
        tokens: int,
        ts: "float | None" = None,
        call_key: "tuple | None" = None,
        cache_tokens: int = 0,
    ) -> None:
        """Record one completed provider attempt's token spend (wall clock).

        ``call_key`` is the durable identity of the metrics record (the
        ``(call_id, attempt)`` pair from llm_call_metrics). A key already
        counted — by this hook or by the restart seed — is skipped, so a
        record cannot double-book the burn. Callers without an identity
        pass None and are always counted. ``cache_tokens`` (2026-10-10
        calibration) is the record's cache_read+cache_creation tokens; it
        only influences the weighted caliber (``_quota_cache_weight``) and
        is inert at the default weight 0.0.
        """

        try:
            tok = int(tokens)
        except (TypeError, ValueError):
            return
        try:
            cache_tok = int(cache_tokens)
        except (TypeError, ValueError):
            cache_tok = 0
        if tok <= 0 and cache_tok <= 0:
            return
        when = time.time() if ts is None else float(ts)
        self._ensure_seeded()
        with self._lock:
            if call_key is not None:
                if call_key in self._seen_call_keys:
                    return
                self._register_call_key_locked(call_key)
            self._events.append((when, tok, cache_tok))
            self._prune_locked(when)

    def _register_call_key_locked(self, call_key: "tuple") -> None:
        seen = self._seen_call_keys
        if seen is None:
            return
        if len(seen) >= self._MAX_CALL_KEYS:
            # Coarse bounded-memory reset: ids are unique per record, so a
            # replay this ancient losing dedupe is harmless (the boot seam
            # the set exists for is always within a few keys of construction).
            seen.clear()
        seen.add(call_key)

    def spend_in_window(self, seconds: float, now: "float | None" = None) -> int:
        """Tokens spent in the trailing ``seconds`` window (base caliber).

        The base caliber is the historical input+output-only total_tokens
        sum — unchanged by the 2026-10-10 cache dimension.
        """

        when = time.time() if now is None else float(now)
        cutoff = when - max(0.0, float(seconds))
        with self._lock:
            return sum(tok for ts, tok, _cache in self._events if ts >= cutoff)

    def spend_weighted_in_window(
        self, seconds: float, now: "float | None" = None
    ) -> float:
        """Window spend with cache tokens weighted by ``_quota_cache_weight``.

        This is the caliber the pace lines consume. At the default weight
        0.0 it is numerically identical to ``spend_in_window``.
        """

        weight = _quota_cache_weight()
        if weight <= 0.0:
            return float(self.spend_in_window(seconds, now=now))
        when = time.time() if now is None else float(now)
        cutoff = when - max(0.0, float(seconds))
        with self._lock:
            return float(
                sum(
                    tok + weight * cache
                    for ts, tok, cache in self._events
                    if ts >= cutoff
                )
            )

    def _prune_locked(self, now: float) -> None:
        window = _quota_window_sec()
        events = self._events
        while events and events[0][0] < now - window:
            events.popleft()
        overflow = len(events) - self._MAX_EVENTS
        for _ in range(max(0, overflow)):
            events.popleft()

    # -- gating -------------------------------------------------------------

    def opportunistic_blocked_scale(self, now: "float | None" = None) -> "float | None":
        """The pace scale (seconds) whose rate line is exhausted, else None.

        ``None`` means the opportunistic lane may launch: either the pacer
        is disabled (budget 0) or every scale's trailing spend is strictly
        below its share of the budget.
        """

        budget = _quota_window_budget_tokens()
        if budget <= 0:
            return None
        # Seed lazily here too (not only in note_usage): the saturator may
        # gate before the first in-process call records usage, and without
        # the seed a fresh process would gate on an empty burn account —
        # exactly the restart-resets-the-clamp hole the seed exists to close.
        self._ensure_seeded()
        window = _quota_window_sec()
        when = time.time() if now is None else float(now)
        rate = float(budget) / window  # tokens/sec mean-rate line
        for scale in (self.SHORT_SCALE_SEC, window):
            span = min(scale, window)
            if self.spend_weighted_in_window(span, now=when) >= rate * span:
                return span
        return None

    def snapshot(self, now: "float | None" = None) -> dict:
        """Observability projection (UI/tests); never a gate input."""

        when = time.time() if now is None else float(now)
        window = _quota_window_sec()
        blocked = self.opportunistic_blocked_scale(now=when)
        return {
            "budget_tokens": _quota_window_budget_tokens(),
            "window_sec": window,
            "spend_1h_tokens": self.spend_in_window(self.SHORT_SCALE_SEC, now=when),
            "spend_window_tokens": self.spend_in_window(window, now=when),
            "opportunistic_blocked_scale_sec": blocked,
            # 2026-10-10 calibration dimension (default weight 0.0 keeps the
            # with-cache caliber identical to the base one).
            "cache_weight": _quota_cache_weight(),
            "spend_1h_tokens_with_cache": int(
                round(self.spend_weighted_in_window(self.SHORT_SCALE_SEC, now=when))
            ),
            "spend_window_tokens_with_cache": int(
                round(self.spend_weighted_in_window(window, now=when))
            ),
        }

    def dual_caliber_snapshot(self, now: "float | None" = None) -> dict:
        """Both cache calibers of the window spend, for 1308 calibration.

        Attached to every ``pipeline.llm_quota_exceeded_detected`` event:
        the provider's 5h usage unit is unknown (observed ~178M ceiling vs
        the 150M budget, both currently no-cache input+output totals), so
        the next real 1308 can compare the provider's own reset-time
        accounting against BOTH ledgers and pin the true unit — after which
        ``POK_LLM_QUOTA_CACHE_WEIGHT`` can be set from evidence.
        """

        when = time.time() if now is None else float(now)
        window = _quota_window_sec()
        return {
            "budget_tokens": _quota_window_budget_tokens(),
            "window_sec": window,
            "cache_weight": _quota_cache_weight(),
            "spend_1h_tokens_without_cache": self.spend_in_window(
                self.SHORT_SCALE_SEC, now=when
            ),
            "spend_window_tokens_without_cache": self.spend_in_window(
                window, now=when
            ),
            "spend_1h_tokens_with_cache": int(
                round(self.spend_weighted_in_window(self.SHORT_SCALE_SEC, now=when))
            ),
            "spend_window_tokens_with_cache": int(
                round(self.spend_weighted_in_window(window, now=when))
            ),
        }

    # -- restart seeding ----------------------------------------------------

    def _ensure_seeded(self) -> None:
        with self._lock:
            if self._seeded:
                return
            self._seeded = True
        self._seed_from_metrics_file()

    def _seed_from_metrics_file(self) -> None:
        path = _quota_pacer_metrics_path()
        if path is None:
            return
        for ts, tok, cache_tok, key in self._read_metrics_tail_events(Path(path)):
            with self._lock:
                if key is not None:
                    self._register_call_key_locked(key)
                self._events.append((ts, tok, cache_tok))
        with self._lock:
            # Hook events are all newer than the seed cutoff (< boot_ts; the
            # epoch_ts == boot_ts rounding edge is left to the hook), but
            # sort anyway so a racing first note_usage cannot disorder the
            # deque; maxlen keeps the NEWEST events under the cap.
            self._events = deque(
                sorted(self._events), maxlen=self._MAX_EVENTS
            )

    def _read_metrics_tail_events(
        self, path: Path
    ) -> "list[tuple[float, int, int, tuple | None]]":
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > self._SEED_MAX_BYTES:
                    handle.seek(max(0, size - self._SEED_MAX_BYTES))
                    handle.readline()  # drop the partial leading line
                raw = handle.read()
        except OSError:
            return []
        cutoff = min(self._boot_ts, time.time()) - _quota_window_sec()
        events: "list[tuple[float, int, int, tuple | None]]" = []
        for line in raw.splitlines()[-self._SEED_MAX_LINES :]:
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    continue
                ts = record.get("epoch_ts")
                tok = record.get("total_tokens")
                if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                    continue
                if not isinstance(tok, int) or tok <= 0:
                    continue
                if float(ts) < cutoff:
                    continue
                if float(ts) >= self._boot_ts:
                    # Post-boot rows belong to THIS process's hook events
                    # (epoch_ts is round(now, 3), so a raw now straddling
                    # the boot stamp resolves to the hook either way — the
                    # >= keeps the rounding edge single-counted); counting
                    # them here too would double-book the burn.
                    continue
                # Cache dimension (2026-10-10): failure rows and retried
                # attempts (attempt > 0) are accounted exactly like any
                # other row that carries usage — the seed has never
                # filtered on success/attempt and must keep not doing so.
                cache_tok = 0
                for cache_field in (
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                ):
                    cache_val = record.get(cache_field)
                    if (
                        isinstance(cache_val, int)
                        and not isinstance(cache_val, bool)
                        and cache_val > 0
                    ):
                        cache_tok += cache_val
                key = None
                call_id = record.get("call_id")
                attempt = record.get("attempt")
                if call_id and isinstance(attempt, int):
                    key = (str(call_id), attempt)
                events.append((float(ts), int(tok), cache_tok, key))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
        return events


#: Injectable metrics-file override (tests); None -> llm_call_metrics default.
_QUOTA_METRICS_FILE_OVERRIDE: "Path | None" = None


def _quota_pacer_metrics_path() -> "Path | None":
    if _QUOTA_METRICS_FILE_OVERRIDE is not None:
        return Path(_QUOTA_METRICS_FILE_OVERRIDE)
    try:
        from llm_call_metrics import _metrics_file

        return _metrics_file()
    except Exception:
        return None


_QUOTA_PACER: "QuotaPacer | None" = None
_QUOTA_PACER_LOCK = threading.Lock()


def get_quota_pacer() -> QuotaPacer:
    """Process-wide burn account (single writer lane, seeded lazily)."""

    global _QUOTA_PACER
    with _QUOTA_PACER_LOCK:
        if _QUOTA_PACER is None:
            _QUOTA_PACER = QuotaPacer()
        return _QUOTA_PACER


def reset_quota_pacer_for_tests() -> None:
    """Drop the singleton so a test can start from a clean burn account."""

    global _QUOTA_PACER
    with _QUOTA_PACER_LOCK:
        _QUOTA_PACER = None


def note_llm_call_tokens(
    tokens: int,
    ts: "float | None" = None,
    call_key: "tuple | None" = None,
    cache_tokens: int = 0,
) -> None:
    """Burn-accounting hook: one completed provider attempt's tokens.

    Called from llm_call_metrics.record_llm_call_metrics — the single seam
    every claude/codex attempt row (successful AND failed-with-usage)
    flows through — with the record's durable ``(call_id, attempt)``
    identity so a row the restart seed already replayed cannot double-book.
    ``cache_tokens`` carries the row's cache dimension for the weighted
    caliber (inert at the default weight 0.0). Best-effort: pacing
    accounting must never affect the dispatch path.
    """

    try:
        get_quota_pacer().note_usage(
            tokens, ts=ts, call_key=call_key, cache_tokens=cache_tokens
        )
    except Exception:
        pass


def quota_pacer_dual_caliber_snapshot(now: "float | None" = None) -> "dict | None":
    """Both cache calibers of the pacer's window spend, or None on failure.

    Consumed by the ``pipeline.llm_quota_exceeded_detected`` emission sites
    (llm_query_retry) so every real 1308 carries the with-cache and
    without-cache ledger snapshots for provider-unit calibration.
    """

    try:
        return get_quota_pacer().dual_caliber_snapshot(now=now)
    except Exception:
        return None


def quota_pacer_opportunistic_block(now: "float | None" = None) -> "float | None":
    """Saturator-facing gate: blocked pace scale in seconds, else None."""

    try:
        return get_quota_pacer().opportunistic_blocked_scale(now=now)
    except Exception:
        return None
