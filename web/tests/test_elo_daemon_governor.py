"""P1 (2026-10-05): load-adaptive rating-daemon worker governor.

Machine load was high with 3 daemon workers (1-minute loadavg 5.1-5.5 on
4 cores, usr 84-90%) while the LLM pipeline must keep >=1 core.  The
governor throttles *new* match dispatch only — the ProcessPoolExecutor
keeps its env-capped size and in-flight 70-hand matches finish naturally
(zero sample loss).  ``POK_DAEMON_PAIRS`` is rating-identity pinned
(evaluation_data_identity runtime_profile) and is never read or written
by the governor; a binding violation fails fast with a typed event and
no dispatch adjustment.

Evidence: F1 (3-worker saturation loadavg 5.5), F5 (1-worker + full-speed
LLM steady-state load 1.62), F6 (citation sample starvation -> floor 1),
operator directive "对局数量应根据机器负载动态调整、LLM 优先".

P2 (2026-10-06, operator directive ②/③ "负载上限提高一倍——1 分钟 load 可
持续 3.0，对局并发在不影响 LLM 产出下动态调整" + "提高并行"): the band
widens to [2.5, 3.0] (upshift threshold 2.0 -> 2.5, downshift semantics
unchanged), the default harness cap mirrors the production
POK_DAEMON_WORKERS=6, and bounds are [1, 6].  Workers (and the governor's
internal pick concurrency) are NOT part of the rating identity
runtime_profile — proven byte-identical below; pairs stays pinned.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import elo_daemon_governor as gov_mod
from elo_daemon_governor import DaemonWorkerGovernor


SAMPLE_INTERVAL = gov_mod.GOVERNOR_SAMPLE_INTERVAL_SEC
MIN_ACTION_GAP = gov_mod.GOVERNOR_MIN_ACTION_INTERVAL_SEC


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class Harness:
    """One governor wired to fake signal files and a fake journal reader.

    Default ``env_workers=6`` mirrors the production cap since P2
    (2026-10-06, operator directive ②/③: deploy/tencent-cloud/env.runtime
    POK_DAEMON_WORKERS=6; governor bounds [1, 6]).
    """

    def __init__(self, tmp_path: Path, *, env_workers: int = 6):
        self.clock = FakeClock()
        self.tmp = tmp_path
        self.events: list[tuple[str, str, dict]] = []
        self.info_lines: list[str] = []
        self.env_workers = env_workers
        self.loadavg = tmp_path / "loadavg"
        self.memory_events = tmp_path / "memory.events"
        self.memory_current = tmp_path / "memory.current"
        self.memory_high = tmp_path / "memory.high"
        self.cpu_pressure = tmp_path / "cpu.pressure"
        self.metrics = tmp_path / "llm_call_metrics.jsonl"
        for name in (
            "loadavg",
            "memory.events",
            "memory.current",
            "memory.high",
            "cpu.pressure",
        ):
            (tmp_path / name).write_text("", encoding="utf-8")
        self.journal_text = ""
        self.gov = DaemonWorkerGovernor(
            env_workers,
            results_dir=tmp_path,
            now=self.clock,
            loadavg_path=self.loadavg,
            memory_events_path=self.memory_events,
            memory_current_path=self.memory_current,
            memory_high_path=self.memory_high,
            cpu_pressure_path=self.cpu_pressure,
            llm_metrics_path=self.metrics,
            journal_reader=self._read_journal,
            event_sink=self._emit,
            logger=self,
        )

    def _read_journal(self, since_sec: float) -> str:
        return self.journal_text

    def _emit(self, event_type: str, severity: str, message: str, data: dict):
        self.events.append((event_type, severity, message, dict(data)))

    def info(self, fmt: str, *args):  # minimal logger seam
        self.info_lines.append(fmt % args if args else fmt)

    # -- signal setters -------------------------------------------------
    def set_load(self, load1: float, load5: float | None = None) -> None:
        self.loadavg.write_text(
            f"{load1:.2f} {(load5 if load5 is not None else load1):.2f} "
            "1.00 1/500 12345\n",
            encoding="utf-8",
        )

    def set_memory(self, *, high_events: int = 0, ratio: float = 0.0) -> None:
        self.memory_events.write_text(
            f"low 0\nhigh {int(high_events)}\nmax 0\noom 0\noom_kill 0\n",
            encoding="utf-8",
        )
        limit = 2_400 * 1024 * 1024
        used = max(0.0, min(1.0, ratio)) * limit
        self.memory_current.write_text(f"{int(used)}\n", encoding="utf-8")
        self.memory_high.write_text(f"{limit}\n", encoding="utf-8")

    def set_cpu_pressure(self, avg10: float) -> None:
        self.cpu_pressure.write_text(
            f"full avg10 {avg10:.2f} avg60 0.00 avg300 0.00 total 0\n",
            encoding="utf-8",
        )

    def append_metric(self, *, timeout_kind: str | None, age_sec: float = 0.0):
        record = {
            "schema_version": 2,
            "ts": "2026-10-05T00:00:00+00:00",
            "epoch_ts": self.clock() - age_sec,
            "role": "master_scout",
            "success": timeout_kind is None,
            "timeout_kind": timeout_kind,
        }
        with self.metrics.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    # -- driver ----------------------------------------------------------
    def tick(self, *, n_pairs: int = 1, advance: float = SAMPLE_INTERVAL):
        self.clock.advance(advance)
        return self.gov.maybe_adjust(n_pairs=n_pairs)

    def decisions(self):
        return [event for event in self.events if event[0] == gov_mod.EVENT_NAME]


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


# ── hysteresis ────────────────────────────────────────────────────────────


def test_single_spike_does_not_downshift(harness):
    harness.set_load(4.61)  # the 18:30:00 single-point spike must not act
    assert harness.tick() == 6
    harness.set_load(1.5)
    assert harness.tick() == 6
    assert harness.gov.effective_workers == 6
    assert harness.decisions() == []


def test_two_consecutive_over_threshold_downshifts_once(harness):
    harness.set_load(5.5)
    harness.tick()
    harness.set_load(5.5)
    workers = harness.tick()
    assert workers == 5
    assert harness.gov.effective_workers == 5
    events = harness.decisions()
    assert len(events) == 1
    _, severity, _message, data = events[0]
    assert data["action"] == "down"
    assert data["reason"] == "load_exceeded_hysteresis"
    assert data["workers_before"] == 6
    assert data["workers_after"] == 5
    assert data["over_threshold_streak"] == 2


def test_three_consecutive_below_threshold_upshifts_once(harness):
    harness.set_load(5.5)
    harness.tick()
    harness.set_load(5.5)
    assert harness.tick() == 5
    harness.set_load(1.0)
    harness.tick()
    harness.set_load(1.0)
    harness.tick()
    assert harness.gov.effective_workers == 5  # two below-samples: no action
    harness.set_load(1.0)
    harness.clock.advance(MIN_ACTION_GAP - 3 * SAMPLE_INTERVAL)
    assert harness.tick() == 6
    events = harness.decisions()
    assert events[-1][3]["action"] == "up"
    assert events[-1][3]["reason"] == "below_threshold_resume"


def test_two_below_samples_do_not_upshift(harness):
    harness.set_load(1.0)
    harness.tick()
    harness.set_load(1.0)
    harness.tick()
    assert harness.gov.effective_workers == 6
    assert harness.decisions() == []


def test_action_min_interval_prevents_oscillation(harness):
    harness.set_load(5.5)
    harness.tick()
    harness.set_load(5.5)
    assert harness.tick() == 5  # t=60: downshift, last action stamped here
    # Below-threshold samples accumulate the up-streak but stay inside the
    # 120s minimum action interval (t=90/120/150, gap from t=60 is only 90s).
    for _ in range(3):
        harness.set_load(0.5)
        harness.tick()
    assert harness.gov.effective_workers == 5
    # Jump well past the 120s gap: the next below-threshold sample acts.
    harness.set_load(0.5)
    harness.clock.advance(MIN_ACTION_GAP)
    assert harness.tick() == 6


def test_within_band_resets_streaks(harness):
    harness.set_load(5.5)
    harness.tick()
    harness.set_load(2.5)  # inside [2.5, 3.0]: resets the over streak
    harness.tick()
    harness.set_load(5.5)
    harness.tick()
    assert harness.gov.effective_workers == 6  # not two consecutive anymore


# ── LLM hard protection ───────────────────────────────────────────────────


def test_llm_stream_protection_bypasses_hysteresis(harness):
    harness.set_load(1.0)  # load is fine — protection must still fire
    harness.append_metric(timeout_kind="stall", age_sec=60.0)
    workers = harness.tick()
    assert workers == 1
    events = harness.decisions()
    assert events[-1][3]["action"] == "down"
    assert events[-1][3]["reason"] == "llm_stream_protection"


def test_llm_protection_first_activity_counts(harness):
    harness.append_metric(timeout_kind="first_activity", age_sec=120.0)
    assert harness.tick() == 1


def test_stale_llm_metric_does_not_protect(harness):
    harness.append_metric(timeout_kind="stall", age_sec=gov_mod.LLM_METRICS_WINDOW_SEC + 120.0)
    harness.set_load(1.0)
    assert harness.tick() == 6


def test_journal_streak_signal_protects(harness):
    harness.journal_text = (
        "saturator pausing launches for 120s after provider failure "
        "(rate-limit/unavailable cooldown (streak 4, exponential backoff))"
    )
    harness.set_load(1.0)
    assert harness.tick() == 1


def test_cpu_pressure_signal_protects(harness):
    harness.set_cpu_pressure(avg10=11.0)
    harness.set_load(1.0)
    assert harness.tick() == 1


def test_protection_recovery_only_via_normal_upshift(harness):
    harness.append_metric(timeout_kind="stall", age_sec=30.0)
    assert harness.tick() == 1
    harness.metrics.write_text("", encoding="utf-8")
    harness.journal_text = ""
    harness.set_load(1.0)
    harness.tick()
    harness.set_load(1.0)
    harness.tick()
    assert harness.gov.effective_workers == 1  # protection resets up-streak
    harness.set_load(1.0)
    harness.clock.advance(MIN_ACTION_GAP - 3 * SAMPLE_INTERVAL)
    assert harness.tick() == 2  # climbs back one step at a time


def test_floor_hold_event_when_pinned_at_one(harness):
    harness.append_metric(timeout_kind="total", age_sec=10.0)
    assert harness.tick() == 1
    # Still overloaded with protection active: stays at the floor and says so.
    harness.set_load(5.5)
    harness.tick()
    events = harness.decisions()
    assert events[-1][3]["action"] == "floor_hold"
    assert harness.gov.effective_workers == 1


# ── memory side signal ────────────────────────────────────────────────────


def test_memory_high_growth_downshifts_after_two_samples(harness):
    harness.set_load(1.0)
    harness.set_memory(high_events=3, ratio=0.5)
    harness.tick()
    assert harness.gov.effective_workers == 6
    harness.set_memory(high_events=9, ratio=0.5)  # counter grew
    workers = harness.tick()
    assert workers == 5
    events = harness.decisions()
    assert events[-1][3]["reason"] == "memory_high"


def test_memory_usage_ratio_counts_as_pressure(harness):
    harness.set_load(1.0)
    harness.set_memory(high_events=0, ratio=0.99)
    assert harness.tick() == 5  # single confirmation: live memory pressure
    assert harness.decisions()[-1][3]["reason"] == "memory_high"


# ── bounds, persistence, pairs pinning ────────────────────────────────────


def test_persisted_state_resumes_after_restart(tmp_path):
    harness = Harness(tmp_path)
    harness.set_load(5.5)
    harness.tick()
    harness.set_load(5.5)
    assert harness.tick() == 5
    state_file = tmp_path / gov_mod.GOVERNOR_STATE_FILENAME
    assert state_file.is_file()
    revived = DaemonWorkerGovernor(
        6,
        results_dir=tmp_path,
        now=FakeClock(harness.clock() + 60.0),
        loadavg_path=harness.loadavg,
        memory_events_path=harness.memory_events,
        memory_current_path=harness.memory_current,
        memory_high_path=harness.memory_high,
        cpu_pressure_path=harness.cpu_pressure,
        llm_metrics_path=harness.metrics,
        journal_reader=lambda since_sec: "",
        event_sink=lambda *a, **k: None,
        logger=None,
    )
    assert revived.effective_workers == 5


def test_persisted_state_clamped_into_bounds(tmp_path):
    # P2 (2026-10-06, operator directive ②/③): bounds are [1, 6] since the
    # env cap rose to POK_DAEMON_WORKERS=6.
    for bad in (0, -3, 7, 99):
        (tmp_path / gov_mod.GOVERNOR_STATE_FILENAME).write_text(
            json.dumps({"effective_workers": bad}), encoding="utf-8"
        )
        revived = DaemonWorkerGovernor(
            6,
            results_dir=tmp_path,
            now=FakeClock(),
            loadavg_path=tmp_path / "loadavg",
            memory_events_path=tmp_path / "memory.events",
            memory_current_path=tmp_path / "memory.current",
            memory_high_path=tmp_path / "memory.high",
            cpu_pressure_path=tmp_path / "cpu.pressure",
            llm_metrics_path=tmp_path / "llm_call_metrics.jsonl",
            journal_reader=lambda since_sec: "",
            event_sink=lambda *a, **k: None,
        )
        assert revived.effective_workers == (1 if bad < 1 else 6)


def test_corrupt_state_file_falls_back_to_env_cap(tmp_path):
    (tmp_path / gov_mod.GOVERNOR_STATE_FILENAME).write_text("{not json", encoding="utf-8")
    revived = DaemonWorkerGovernor(
        6,
        results_dir=tmp_path,
        now=FakeClock(),
        loadavg_path=tmp_path / "loadavg",
        memory_events_path=tmp_path / "memory.events",
        memory_current_path=tmp_path / "memory.current",
        memory_high_path=tmp_path / "memory.high",
        cpu_pressure_path=tmp_path / "cpu.pressure",
        llm_metrics_path=tmp_path / "llm_call_metrics.jsonl",
        journal_reader=lambda since_sec: "",
        event_sink=lambda *a, **k: None,
    )
    assert revived.effective_workers == 6


def test_env_workers_floor_is_one(tmp_path):
    harness = Harness(tmp_path, env_workers=1)
    harness.set_load(9.9)
    harness.tick()
    harness.set_load(9.9)
    assert harness.tick() == 1
    assert harness.decisions()[-1][3]["action"] == "floor_hold"


def test_pairs_binding_violation_fails_fast_without_action(harness):
    harness.set_load(5.5)
    assert harness.tick(n_pairs=1) == 6  # first sample only
    harness.set_load(5.5)
    workers = harness.tick(n_pairs=2)  # pairs drifted mid-flight
    assert workers == 6  # no adjustment applied
    violation = [e for e in harness.events if "pairs_violation" in e[0]]
    assert violation, "expected a typed pairs-violation event"
    assert violation[0][3]["n_pairs_before"] == 1
    assert violation[0][3]["n_pairs_after"] == 2


def test_stable_pairs_keeps_governing(harness):
    harness.set_load(5.5)
    harness.tick(n_pairs=1)
    harness.set_load(5.5)
    assert harness.tick(n_pairs=1) == 5


def test_unreadable_signals_are_neutral(harness):
    harness.loadavg.write_text("garbage", encoding="utf-8")
    harness.set_cpu_pressure(avg10=0.0)
    assert harness.tick() == 6
    assert harness.decisions() == []


def test_events_carry_full_typed_payload(harness):
    harness.set_load(5.5)
    harness.tick()
    harness.set_load(5.5)
    harness.tick()
    data = harness.decisions()[0][3]
    for key in (
        "action",
        "reason",
        "load1",
        "load5",
        "workers_before",
        "workers_after",
        "over_threshold_streak",
        "below_threshold_streak",
        "memory_events_high",
        "memory_usage_ratio",
        "cpu_pressure_avg10",
        "env_workers_cap",
    ):
        assert key in data, f"missing event field {key}"
    assert harness.info_lines, "expected one Chinese log_history line per action"


# ── daemon wiring contract (source-level) ─────────────────────────────────


def test_daemon_wiring_gates_dispatch_not_pool_size():
    source = (Path(__file__).resolve().parents[1] / "core" / "elo_daemon.py").read_text(
        encoding="utf-8"
    )
    # The executor keeps the env-capped pool size; only the dispatch gate reads
    # the governor.
    assert "ProcessPoolExecutor(max_workers=n_workers, mp_context=mp_ctx)" in source
    assert source.count("governor.effective_workers") >= 3, (
        "expected the three dispatch gates (seed/refill/recovery) plus the "
        "post-completion replenish to read governor.effective_workers"
    )
    assert "governor.maybe_adjust(n_pairs=n_pairs)" in source


# ── completion-path replenish behaviour (B1 red-team follow-up) ───────────


class _FakeExecutor:
    def __init__(self):
        self.submitted = []

    def submit(self, fn, job):
        self.submitted.append(job)

        class _Fut:
            pass

        return _Fut()


class _GateHarness:
    """Drive the extracted completion-path scheduling seam in isolation."""

    def __init__(self, monkeypatch, *, gate, in_flight_count, queue_pairs):
        import collections
        import elo_daemon

        self.elo = elo_daemon
        self.executor = _FakeExecutor()
        self.in_flight = {object(): ("national_cloud_v1", "national_cloud_v2")
                          for _ in range(in_flight_count)}
        self.queue = collections.deque()
        for a, b in queue_pairs:
            self.queue.append((a, b))
        self.pick_calls = []

        monkeypatch.setattr(
            elo_daemon, "pick_matches",
            lambda active_bots, h2h, ratings, n_picks=None: (
                self.pick_calls.append(n_picks) or [("national_cloud_v1", "national_cloud_v3")]
            ),
        )
        monkeypatch.setattr(
            elo_daemon, "_safe_bot_path", lambda bot, verbose=False: f"/bots/{bot}"
        )
        monkeypatch.setattr(
            elo_daemon, "run_single_match", lambda job: ("a", "b", 0, 0, 0, 0, None, [])
        )

    def run(self, gate):
        self.elo._replenish_after_completion(
            match_queue=self.queue,
            in_flight=self.in_flight,
            gate=gate,
            executor=self.executor,
            active_bots={
                "national_cloud_v1", "national_cloud_v2", "national_cloud_v3"
            },
            ratings={},
            h2h={},
            n_pairs=1,
            verbose=False,
        )


def test_replenish_closed_gate_never_dispatches_even_when_queue_empty(monkeypatch):
    """B1 (red-team): gate=1 with 3 in-flight must not submit after refill.

    The pre-fix ``elif`` branch refilled the queue and immediately
    dispatched one match with no gate check, so a downshift (or LLM hard
    protection) never reduced the real match parallelism and the queue grew
    without bound (simulated: +1 per completion at gate=1, +3 at gate=2).
    """
    harness = _GateHarness(
        monkeypatch, gate=1, in_flight_count=3, queue_pairs=[]
    )
    harness.run(gate=1)
    assert harness.executor.submitted == [], "closed gate must not dispatch"
    # Refill itself stays bounded by gate*2 (no unbounded queue growth).
    assert harness.pick_calls == [2]
    assert len(harness.queue) == 1  # one refill pair appended, none consumed


def test_replenish_open_gate_dispatches_one(monkeypatch):
    harness = _GateHarness(
        monkeypatch, gate=3, in_flight_count=2, queue_pairs=[]
    )
    harness.run(gate=3)
    assert len(harness.executor.submitted) == 1
    assert len(harness.in_flight) == 3


def test_replenish_does_not_refill_nonempty_queue(monkeypatch):
    harness = _GateHarness(
        monkeypatch, gate=3, in_flight_count=3,
        queue_pairs=[("national_cloud_v1", "national_cloud_v2")],
    )
    harness.run(gate=3)
    assert harness.pick_calls == []  # queue already has work: no pick_matches
    assert harness.executor.submitted == []  # gate closed (3 in-flight == gate)


def test_replenish_open_gate_with_queued_match_consumes_it(monkeypatch):
    harness = _GateHarness(
        monkeypatch, gate=3, in_flight_count=1,
        queue_pairs=[("national_cloud_v1", "national_cloud_v2")],
    )
    harness.run(gate=3)
    assert harness.pick_calls == []
    assert len(harness.executor.submitted) == 1
    assert len(harness.queue) == 0


def test_replenish_skips_reaped_bots_without_dispatch(monkeypatch):
    harness = _GateHarness(
        monkeypatch, gate=3, in_flight_count=1,
        queue_pairs=[("national_cloud_v1", "national_cloud_reaped")],
    )
    harness.run(gate=3)
    assert harness.executor.submitted == []


def test_replenish_closed_gate_stays_bounded_across_completions(monkeypatch):
    """Red-team simulation: gate=1 after a downshift from 3 in-flight.

    Pre-fix behaviour (simulated by the red team): every completion ran the
    ungated ``elif`` — one match submitted per completion (parallelism
    stayed 3) and the queue grew +1 net each time (gate=2: +3). Post-fix:
    zero submits and the queue stabilises at the bounded refill size.
    """
    harness = _GateHarness(
        monkeypatch, gate=1, in_flight_count=3, queue_pairs=[]
    )
    for _ in range(10):  # ten consecutive completions
        harness.run(gate=1)
    assert harness.executor.submitted == []
    assert len(harness.pick_calls) == 1  # refilled once, then queue non-empty
    assert len(harness.queue) == 1  # bounded; no net growth over completions
    assert len(harness.in_flight) == 3  # untouched: in-flight finish naturally


def test_daemon_replenish_block_delegates_to_extracted_seam():
    source = (Path(__file__).resolve().parents[1] / "core" / "elo_daemon.py").read_text(
        encoding="utf-8"
    )
    assert "_replenish_after_completion(" in source
    # The B1 hole: no unconditional completion-path dispatch remains.
    assert "elif executor is not None:" not in source


# ── P2 (2026-10-06): load ceiling doubled — band [2.5, 3.0], env cap 6 ─────
#
# 操作员指令②（2026-10-06）: "负载上限提高一倍——1 分钟 load 可持续 3.0，
# 对局并发在不影响 LLM 产出下动态调整"；指令③: 提高并行。落到 governor：
# DOWN 语义不变（>3.0 连续 2 次降 1），UP 阈值 2.0 -> 2.5（<2.5 连续 3 次
# 升 1），env cap POK_DAEMON_WORKERS 3 -> 6（deploy/tencent-cloud/env.runtime），
# governor 边界 [1, 6]。LLM 硬保护（流停滞/超时 -> 立即降 1）不变。


def test_load_below_2_5_upshifts_after_three_samples(tmp_path):
    """P2 区分性测试：2.4 在旧 UP 阈值 (2.0) 的带内不可升，新语义必须升。

    旧实现下 load=2.4 属带内稳定值（streak 归零），三次 below 不升档；
    UP 阈值提高到 2.5 后第三次 below 样本必须升 1。
    """
    harness = Harness(tmp_path, env_workers=6)
    harness.set_load(5.5)
    harness.tick()
    harness.set_load(5.5)
    assert harness.tick() == 5  # >3.0 twice: downshift within [1, 6]
    harness.set_load(2.4)
    harness.tick()
    harness.set_load(2.4)
    assert harness.tick() == 5  # only two below-samples: no action yet
    harness.set_load(2.4)
    harness.clock.advance(MIN_ACTION_GAP - 3 * SAMPLE_INTERVAL)
    assert harness.tick() == 6  # third below-2.5 sample climbs back


def test_load_2_5_is_inside_band_not_below(tmp_path):
    """P2 带下沿锁定：2.5 恰在带内 [2.5, 3.0]（非严格 < 2.5），不升。"""
    harness = Harness(tmp_path, env_workers=6)
    harness.set_load(5.5)
    harness.tick()
    harness.set_load(5.5)
    assert harness.tick() == 5
    for _ in range(2):
        harness.set_load(2.5)
        harness.tick()
    harness.set_load(2.5)
    harness.clock.advance(MIN_ACTION_GAP - 3 * SAMPLE_INTERVAL)
    assert harness.tick() == 5  # 2.5 is not below the up threshold
    assert harness.gov.effective_workers == 5


def test_load_exactly_3_0_is_sustainable_no_downshift(tmp_path):
    """P2 带上沿锁定（操作员指令②"1 分钟 load 可持续 3.0"）：3.0 不降档。"""
    harness = Harness(tmp_path, env_workers=6)
    harness.set_load(3.0)
    harness.tick()
    harness.set_load(3.0)
    assert harness.tick() == 6  # 3.0 <= 3.0: sustainable, no downshift
    assert harness.gov.effective_workers == 6
    harness.set_load(3.1)
    harness.tick()
    harness.set_load(3.1)
    assert harness.tick() == 5  # >3.0 twice: downshift (unchanged semantics)


def test_upshift_stops_at_env_cap_six(tmp_path):
    """P2 边界 [1, 6]：已在 env cap 6 时低载不越界（也不发事件）。"""
    harness = Harness(tmp_path, env_workers=6)
    harness.set_load(1.0)
    harness.tick()
    harness.set_load(1.0)
    harness.tick()
    harness.set_load(1.0)
    harness.clock.advance(MIN_ACTION_GAP - 3 * SAMPLE_INTERVAL)
    assert harness.tick() == 6  # already at cap: clamp holds
    assert harness.decisions() == []


def test_workers_change_keeps_rating_identity_profile_unchanged(tmp_path, monkeypatch):
    """P2 身份不变性（操作员指令② + 身份约束，2026-10-06）。

    生产同源路径：runtime_profile 由 ``elo_daemon._rating_protocol_config``
    构造、经 ``ensure_evaluation_data_identity`` 绑进
    evaluation_data_manifest.json（web/core/elo_daemon.py:1367-1373）。
    POK_DAEMON_WORKERS 3 -> 6（以及 governor 的 effective_workers /
    内部选对并发 n_picks）不进入该构造，manifest 摘要必须逐字节不变；
    n_pairs 改变必须改变 profile 并触发身份错误（检出力对照，也是
    pairs 禁止运行时修改的原因）。
    """
    import evaluation_data_identity as identity
    from bot_artifact import canonical_digest

    import elo_daemon

    monkeypatch.delenv("POK_NATIONAL_RATING_MATCHES", raising=False)
    monkeypatch.setenv("POK_DAEMON_WORKERS", "3")
    profile_workers3 = elo_daemon._rating_protocol_config(n_pairs=1)
    monkeypatch.setenv("POK_DAEMON_WORKERS", "6")
    profile_workers6 = elo_daemon._rating_protocol_config(n_pairs=1)
    # 逐字节不变：生产 digest 函数 + 序列化字节都一致。
    assert canonical_digest(profile_workers3) == canonical_digest(profile_workers6)
    assert (
        json.dumps(profile_workers3, sort_keys=True, ensure_ascii=False)
        == json.dumps(profile_workers6, sort_keys=True, ensure_ascii=False)
    )

    results = tmp_path / "results"
    manifest_3 = identity.ensure_evaluation_data_identity(
        results, runtime_profile=profile_workers3
    )
    # workers=6 的 profile 必须被既有 manifest 原样接受（不触发归档重置）。
    manifest_6 = identity.ensure_evaluation_data_identity(
        results, runtime_profile=profile_workers6
    )
    assert manifest_6["manifest_digest"] == manifest_3["manifest_digest"]

    # 检出力对照：n_pairs=2 改变 profile，且生产校验必须报身份漂移。
    profile_pairs2 = elo_daemon._rating_protocol_config(n_pairs=2)
    assert canonical_digest(profile_pairs2) != canonical_digest(profile_workers6)
    with pytest.raises(
        identity.EvaluationDataIdentityError, match="runtime profile changed"
    ):
        identity.ensure_evaluation_data_identity(
            results, runtime_profile=profile_pairs2
        )


def test_governor_never_touches_pairs_identity():
    import ast

    source = Path(gov_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    code_strings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    code_names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    # Docstrings/comments may *mention* the pinned knob; executable code may
    # never read, write, or construct it.
    assert "POK_DAEMON_PAIRS" not in code_strings
    assert "runtime_profile" not in code_strings
    assert "POK_DAEMON_PAIRS" not in code_names
    assert "--pairs" not in code_strings
