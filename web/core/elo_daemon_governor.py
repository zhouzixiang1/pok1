"""Load-adaptive worker governor for the rating daemon (P1, 2026-10-05).

Operator directive: 对局数量应根据机器负载动态调整，LLM 优先于对局.
Machine evidence: with 3 daemon workers the 1-minute loadavg hit 5.1-5.5 on
4 cores (usr 84-90%) while the LLM pipeline needs >= 1 core; with 1 worker
plus full-speed LLM the steady-state load was 1.62 — a feasible operating
point.  The governor therefore throttles *new* match dispatch only:

* The ``ProcessPoolExecutor`` keeps its env-capped ``max_workers`` (a live
  pool cannot be resized); the governor's ``effective_workers`` value is a
  *dispatch gate* — ``while len(in_flight) < governor.effective_workers``.
  Shrinking never kills an in-flight 70-hand match; it finishes naturally
  and is admitted exactly as before (zero sample loss).
* Signals: ``/proc/loadavg`` 1-minute value (whole-machine — it already
  includes LLM streams, which is exactly why it is the right signal),
  cgroup ``memory.events`` high-counter growth / ``memory.current`` vs
  ``memory.high``, and cgroup ``cpu.pressure`` avg10 as a direct
  LLM-stream-squeeze indicator.  Sampling runs inside the daemon's single
  scheduling loop every ``GOVERNOR_SAMPLE_INTERVAL_SEC`` (30s) and is a
  ~microsecond read.
* Hysteresis: downshift after 2 consecutive samples above
  ``GOVERNOR_LOAD1_DOWN_THRESHOLD`` (>=60s sustained), upshift after 3
  consecutive samples below ``GOVERNOR_LOAD1_UP_THRESHOLD``; a single spike
  never acts.  Same-direction actions are >=120s apart.
* P2 (2026-10-06, operator directive ②/③): the sustained 1-minute load
  target is 3.0 (doubled ceiling — load may sustainably run at 3.0), the
  band is [2.5, 3.0], and the env cap rises 3 -> 6 with bounds [1, 6].
  DOWN semantics (>3.0 twice -> -1) and the LLM hard protection below are
  unchanged.
* Bounds: ``[1, env POK_DAEMON_WORKERS]``.  The floor is 1, never 0 —
  Master citations need fresh match samples (2026-10-05:
  ``best_available=2`` starved the v512 channel for 4h20m).
* LLM hard protection (bypasses hysteresis): recent LLM timeout metrics
  (``timeout_kind`` non-empty), journal evidence of saturator streak >=4
  cooldowns / an orchestrator "retry loop stopped" line, or
  ``cpu.pressure`` avg10 > 10 immediately drops the gate to 1 and resets
  the upshift streak; recovery climbs only through the normal upshift path.
* ``POK_DAEMON_PAIRS`` is rating-identity pinned (the daemon's
  ``runtime_profile`` inside ``evaluation_data_identity``; a runtime change
  is a deterministic daemon crash).  This module never reads or writes it,
  never touches the daemon argv ``--pairs`` component, and never touches
  any ``runtime_profile`` field.  ``maybe_adjust`` binds the first
  observed ``n_pairs`` and fails fast with a typed event — no dispatch
  adjustment — if a later call reports a different value.

State persists to ``results/daemon_worker_governor_state.json`` so a
restarted daemon resumes from the persisted gate instead of immediately
re-saturating at the env cap; the env value is only the cap and the
no-state-file initial value.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("pok.daemon.governor")

# ── tunables (module constants; tests inject paths/clock instead) ─────────

GOVERNOR_SAMPLE_INTERVAL_SEC = float(
    os.environ.get("POK_GOVERNOR_SAMPLE_INTERVAL_SEC", "30")
)
#: 1-minute load target band: sustained ceiling 3.0, recovery floor 2.5.
#: P2 (2026-10-06, operator directive ② "负载上限提高一倍——1 分钟 load
#: 可持续 3.0，对局并发在不影响 LLM 产出下动态调整"): the ceiling stays
#: cores-1 (4-core host -> 3.0) but is now a *sustainable* target, and the
#: upshift threshold rises 2.0 -> 2.5 so the governor holds the widened
#: band [2.5, 3.0] instead of racing back down at the old 2.0 edge.  The
#: env cap rises 3 -> 6 (deploy/tencent-cloud/env.runtime
#: POK_DAEMON_WORKERS=6, directive ③ 提高并行), so bounds are [1, 6].
GOVERNOR_LOAD1_DOWN_THRESHOLD = 3.0
GOVERNOR_LOAD1_UP_THRESHOLD = 2.5
GOVERNOR_DOWN_CONSECUTIVE = 2
GOVERNOR_UP_CONSECUTIVE = 3
GOVERNOR_MIN_ACTION_INTERVAL_SEC = 120.0
#: memory.current/memory.high ratio that counts as memory pressure.
GOVERNOR_MEMORY_PRESSURE_RATIO = 0.95
#: cpu.pressure avg10 above which LLM streams count as CPU-squeezed.
GOVERNOR_CPU_PRESSURE_AVG10 = 10.0
#: llm_call_metrics lookback for hard protection (signal a).
LLM_METRICS_WINDOW_SEC = 600.0
#: journal lookback for hard protection (signal b).
LLM_JOURNAL_WINDOW_SEC = 300.0
#: journal unit inspected for saturator/orchestrator LLM stall evidence.
GOVERNOR_JOURNAL_UNIT_ENV = "POK_GOVERNOR_JOURNAL_UNIT"
_DEFAULT_JOURNAL_UNIT = "pok-evolution"

GOVERNOR_STATE_FILENAME = "daemon_worker_governor_state.json"
GOVERNOR_EVENT_NAME = "pipeline.daemon_worker_governor"
#: Short alias used by tests and journal greps.
EVENT_NAME = GOVERNOR_EVENT_NAME
GOVERNOR_PAIRS_VIOLATION_EVENT = "pipeline.daemon_worker_governor_pairs_violation"

_JOURNAL_STREAK_RE = re.compile(r"streak (\d+)")
_JOURNAL_STOPPED_MARKERS = ("retry loop stopped",)
#: bytes read from the tail of llm_call_metrics.jsonl (bounded, ~2k calls).
_LLM_METRICS_TAIL_BYTES = 256 * 1024


def _default_event_sink(event_type, severity, message, data=None):
    try:
        from system_log import log_system_event

        log_system_event(event_type, severity, message, data)
    except Exception:  # pragma: no cover - event ledger must never break gating
        log.warning("governor event emission failed: %s", event_type)


def _resolve_cgroup_dir() -> Path:
    """Locate this process's cgroup v2 directory (operator-overridable)."""

    override = os.environ.get("POK_GOVERNOR_CGROUP_DIR")
    if override:
        return Path(override)
    try:
        for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[0] == "0" and parts[2].strip():
                candidate = Path("/sys/fs/cgroup") / parts[2].strip().lstrip("/")
                if (candidate / "memory.events").exists():
                    return candidate
    except OSError:
        pass
    return Path("/sys/fs/cgroup/system.slice/pok-evolution.service")


def _read_loadavg(path: Path) -> tuple[float | None, float | None]:
    try:
        fields = Path(path).read_text(encoding="utf-8").split()
        return float(fields[0]), float(fields[1])
    except (OSError, ValueError, IndexError):
        return None, None


def _read_memory_events_high(path: Path) -> int | None:
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "high":
                return int(parts[1])
    except (OSError, ValueError):
        pass
    return None


def _read_int_file(path: Path) -> int | None:
    try:
        return int(Path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _read_cpu_pressure_avg10(path: Path) -> float | None:
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.startswith("full ") or line.startswith("some "):
                fields = line.split()
                if "avg10" in fields:
                    return float(fields[fields.index("avg10") + 1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _default_journal_reader(since_sec: float) -> str:
    """Best-effort journalctl read; any failure yields empty text."""

    unit = os.environ.get(GOVERNOR_JOURNAL_UNIT_ENV) or _DEFAULT_JOURNAL_UNIT
    minutes = max(1, int(since_sec // 60) + 1)
    try:
        completed = subprocess.run(
            [
                "journalctl",
                "-u",
                unit,
                "--since",
                f"-{minutes} min",
                "--no-pager",
                "-n",
                "800",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return completed.stdout or ""
    except Exception:  # pragma: no cover - journal may not exist in tests
        return ""


def _llm_metrics_timeout_recent(path: Path, now_ts: float) -> bool:
    """True when a recent metrics row carries a non-empty ``timeout_kind``."""

    try:
        with Path(path).open("rb") as handle:
            try:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - _LLM_METRICS_TAIL_BYTES))
                tail = handle.read().decode("utf-8", errors="replace")
            except OSError:
                return False
    except OSError:
        return False
    lines = tail.splitlines()
    if size > _LLM_METRICS_TAIL_BYTES and lines:
        lines = lines[1:]  # drop the likely-partial first line
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        try:
            row_ts = float(record.get("epoch_ts") or 0.0)
        except (TypeError, ValueError):
            row_ts = 0.0
        if row_ts and now_ts - row_ts > LLM_METRICS_WINDOW_SEC:
            # Rows are appended chronologically; the first stale row from the
            # end bounds the scan.
            break
        if record.get("timeout_kind"):
            return True
    return False


def _journal_llm_stall(text: str) -> bool:
    if not text:
        return False
    for marker in _JOURNAL_STOPPED_MARKERS:
        if marker in text:
            return True
    for value in _JOURNAL_STREAK_RE.findall(text):
        try:
            if int(value) >= 4:
                return True
        except ValueError:
            continue
    return False


class DaemonWorkerGovernor:
    """Dispatch-gate state machine; runs inside the daemon's main loop."""

    def __init__(
        self,
        env_workers: int,
        *,
        results_dir: Path | str,
        now: Callable[[], float] | None = None,
        loadavg_path: Path | str | None = None,
        memory_events_path: Path | str | None = None,
        memory_current_path: Path | str | None = None,
        memory_high_path: Path | str | None = None,
        cpu_pressure_path: Path | str | None = None,
        llm_metrics_path: Path | str | None = None,
        journal_reader: Callable[[float], str] | None = None,
        event_sink: Callable | None = None,
        logger=None,
    ):
        self.env_workers = max(1, int(env_workers))
        self.results_dir = Path(results_dir)
        self._now = now or time.time
        self._logger = logger if logger is not None else log

        cgroup = _resolve_cgroup_dir()
        self._loadavg_path = Path(loadavg_path or "/proc/loadavg")
        self._memory_events_path = Path(memory_events_path or (cgroup / "memory.events"))
        self._memory_current_path = Path(
            memory_current_path or (cgroup / "memory.current")
        )
        self._memory_high_path = Path(memory_high_path or (cgroup / "memory.high"))
        self._cpu_pressure_path = Path(cpu_pressure_path or (cgroup / "cpu.pressure"))
        self._llm_metrics_path = Path(
            llm_metrics_path or (self.results_dir / "llm_call_metrics.jsonl")
        )
        self._journal_reader = journal_reader or _default_journal_reader
        self._event_sink = event_sink or _default_event_sink

        self._effective = self._load_persisted()
        self._last_sample_ts: float = -1e18
        self._last_action_ts: float = -1e18
        self._over_streak = 0
        self._under_streak = 0
        self._last_memory_high: int | None = None
        # Pairs pinning: the first observed n_pairs is the binding; any later
        # value difference is a fail-fast violation (typed event, no action).
        self._bound_n_pairs: int | None = None

    # ── state persistence ─────────────────────────────────────────────

    @property
    def state_path(self) -> Path:
        return self.results_dir / GOVERNOR_STATE_FILENAME

    def _load_persisted(self) -> int:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            persisted = int(data.get("effective_workers"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return self.env_workers
        return self._clamp(persisted)

    def _clamp(self, value: int) -> int:
        return max(1, min(self.env_workers, int(value)))

    def _persist(self) -> None:
        try:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "effective_workers": self._effective,
                "env_workers_cap": self.env_workers,
                "updated_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            }
            temporary = self.state_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.state_path)
        except OSError:
            pass  # persistence is best-effort; the in-memory gate still rules

    # ── sampling ──────────────────────────────────────────────────────

    def sample(self) -> dict:
        load1, load5 = _read_loadavg(self._loadavg_path)
        memory_high = _read_memory_events_high(self._memory_events_path)
        memory_current = _read_int_file(self._memory_current_path)
        memory_limit = _read_int_file(self._memory_high_path)
        usage_ratio = None
        if memory_current is not None and memory_limit:
            usage_ratio = round(memory_current / memory_limit, 4)
        return {
            "load1": load1,
            "load5": load5,
            "memory_events_high": memory_high,
            "memory_usage_ratio": usage_ratio,
            "cpu_pressure_avg10": _read_cpu_pressure_avg10(
                self._cpu_pressure_path
            ),
        }

    def _llm_protection_active(self, sample: dict) -> bool:
        now_ts = self._now()
        if _llm_metrics_timeout_recent(self._llm_metrics_path, now_ts):
            return True
        if _journal_llm_stall(self._journal_reader(LLM_JOURNAL_WINDOW_SEC)):
            return True
        avg10 = sample.get("cpu_pressure_avg10")
        if avg10 is not None and float(avg10) > GOVERNOR_CPU_PRESSURE_AVG10:
            return True
        return False

    # ── decision ──────────────────────────────────────────────────────

    @property
    def effective_workers(self) -> int:
        return self._effective

    def maybe_adjust(self, *, n_pairs: int) -> int:
        """One non-blocking scheduling-loop call; internally 30s-throttled."""

        pairs_before = int(n_pairs)
        if self._bound_n_pairs is None:
            self._bound_n_pairs = pairs_before
        elif pairs_before != self._bound_n_pairs:
            self._emit(
                GOVERNOR_PAIRS_VIOLATION_EVENT,
                "error",
                "daemon worker governor refused to adjust: n_pairs changed "
                f"({self._bound_n_pairs} -> {pairs_before}); pairs is "
                "rating-identity pinned and must never be touched at runtime",
                {
                    "n_pairs_before": self._bound_n_pairs,
                    "n_pairs_after": pairs_before,
                    "effective_workers": self._effective,
                },
            )
            return self._effective

        now_ts = self._now()
        if now_ts - self._last_sample_ts < GOVERNOR_SAMPLE_INTERVAL_SEC:
            return self._effective
        self._last_sample_ts = now_ts

        snapshot = self.sample()

        # LLM hard protection bypasses hysteresis entirely and drops the gate
        # straight to the floor (operator directive: LLM 优先于对局).
        if self._llm_protection_active(snapshot):
            self._under_streak = 0
            self._over_streak = 0
            if self._effective > 1:
                self._apply(
                    1,
                    action="down",
                    reason="llm_stream_protection",
                    snapshot=snapshot,
                )
            else:
                self._emit_decision(
                    "floor_hold", "llm_stream_protection", snapshot
                )
            return self._effective

        load1 = snapshot.get("load1")

        # Memory side signal: cgroup memory.events high-counter growth or a
        # near-limit current/high ratio triggers the downshift immediately
        # (single-confirmation: a live throttling event is real pressure,
        # not load noise) — but never below the floor of 1.
        memory_pressure = False
        high_events = snapshot.get("memory_events_high")
        if high_events is not None:
            if (
                self._last_memory_high is not None
                and int(high_events) > self._last_memory_high
            ):
                memory_pressure = True
            self._last_memory_high = int(high_events)
        ratio = snapshot.get("memory_usage_ratio")
        if ratio is not None and float(ratio) >= GOVERNOR_MEMORY_PRESSURE_RATIO:
            memory_pressure = True

        if memory_pressure:
            self._over_streak = 0
            self._under_streak = 0
            if self._effective > 1:
                self._apply(
                    self._effective - 1,
                    action="down",
                    reason="memory_high",
                    snapshot=snapshot,
                )
            else:
                self._emit_decision("floor_hold", "memory_high", snapshot)
            return self._effective

        over = load1 is not None and float(load1) > GOVERNOR_LOAD1_DOWN_THRESHOLD
        under = load1 is not None and float(load1) < GOVERNOR_LOAD1_UP_THRESHOLD

        if over:
            self._over_streak += 1
            self._under_streak = 0
            if self._over_streak >= GOVERNOR_DOWN_CONSECUTIVE:
                if self._effective > 1 and self._action_gap_ok(now_ts):
                    self._apply(
                        self._effective - 1,
                        action="down",
                        reason="load_exceeded_hysteresis",
                        snapshot=snapshot,
                    )
                elif self._effective <= 1:
                    self._emit_decision(
                        "floor_hold",
                        "load_exceeded_hysteresis",
                        snapshot,
                    )
            return self._effective

        if under:
            self._under_streak += 1
            self._over_streak = 0
            if (
                self._under_streak >= GOVERNOR_UP_CONSECUTIVE
                and self._effective < self.env_workers
                and self._action_gap_ok(now_ts)
            ):
                self._apply(
                    self._effective + 1,
                    action="up",
                    reason="below_threshold_resume",
                    snapshot=snapshot,
                )
            return self._effective

        # Inside the target band: everything settles.
        self._over_streak = 0
        self._under_streak = 0
        return self._effective

    def _action_gap_ok(self, now_ts: float) -> bool:
        return (now_ts - self._last_action_ts) >= GOVERNOR_MIN_ACTION_INTERVAL_SEC

    def _apply(self, new_workers: int, *, action: str, reason: str, snapshot: dict):
        before = self._effective
        self._effective = self._clamp(new_workers)
        self._last_action_ts = self._now()
        self._persist()
        self._emit_decision(action, reason, snapshot, workers_before=before)
        try:
            self._logger.info(
                "调速器: 对局并行度 %d→%d (%s, load1=%s, load5=%s)",
                before,
                self._effective,
                reason,
                snapshot.get("load1"),
                snapshot.get("load5"),
            )
        except Exception:
            pass

    def _emit_decision(
        self,
        action: str,
        reason: str,
        snapshot: dict,
        *,
        workers_before: int | None = None,
    ):
        data = {
            "action": action,
            "reason": reason,
            "load1": snapshot.get("load1"),
            "load5": snapshot.get("load5"),
            "workers_before": (
                workers_before if workers_before is not None else self._effective
            ),
            "workers_after": self._effective,
            "over_threshold_streak": self._over_streak,
            "below_threshold_streak": self._under_streak,
            "memory_events_high": snapshot.get("memory_events_high"),
            "memory_usage_ratio": snapshot.get("memory_usage_ratio"),
            "cpu_pressure_avg10": snapshot.get("cpu_pressure_avg10"),
            "env_workers_cap": self.env_workers,
            "sample_interval_sec": GOVERNOR_SAMPLE_INTERVAL_SEC,
            "min_action_interval_sec": GOVERNOR_MIN_ACTION_INTERVAL_SEC,
            "n_pairs": self._bound_n_pairs,
        }
        self._emit(
            GOVERNOR_EVENT_NAME,
            "info",
            f"daemon worker governor {action}: {reason} "
            f"(workers {data['workers_before']}->{data['workers_after']}, "
            f"load1={data['load1']})",
            data,
        )

    def _emit(self, event_type: str, severity: str, message: str, data: dict):
        try:
            self._event_sink(event_type, severity, message, data)
        except Exception:
            pass


__all__ = [
    "DaemonWorkerGovernor",
    "GOVERNOR_SAMPLE_INTERVAL_SEC",
    "GOVERNOR_MIN_ACTION_INTERVAL_SEC",
    "GOVERNOR_LOAD1_DOWN_THRESHOLD",
    "GOVERNOR_LOAD1_UP_THRESHOLD",
    "GOVERNOR_EVENT_NAME",
    "GOVERNOR_STATE_FILENAME",
    "LLM_METRICS_WINDOW_SEC",
]
