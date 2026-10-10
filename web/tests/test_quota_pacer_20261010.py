"""5h rolling-window quota pacing for the opportunistic LLM lane (w2/w4/w5).

GLM 1308 caps usage over a rolling ~5h window (~178M observed capacity);
unpaced saturator burn (16-40M tok/h) front-loaded every window, punched
through the cap, and the provider quota pause then zeroed ALL dispatch for
the window tail (whole zero-token hours on 10-08/09/10).

Contract (2026-10-10, ``llm_concurrency.QuotaPacer``):

* Disabled by default: ``POK_LLM_QUOTA_WINDOW_BUDGET_TOKENS`` unset/0
  never gates, regardless of recorded burn.
* Enabled: the trailing-1h spend is clamped to budget/5 (kills the
  front-load sawtooth) and the trailing-window spend to the full budget;
  hitting a line blocks the opportunistic lane (saturator reason
  ``quota_pacing``), and the block clears as events age out of the scale.
* Burn accounting is fed by ``llm_call_metrics.record_llm_call_metrics``
  (every completed claude/codex attempt) and seeded from the durable
  metrics tail at process start, so a restart does not reset the clamp.
* Pipeline roles are never gated (the saturator is the only consumer of
  the block predicate).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import llm_call_metrics
import llm_concurrency
import llm_saturator
from llm_concurrency import (
    QuotaPacer,
    get_quota_pacer,
    note_llm_call_tokens,
    reset_quota_pacer_for_tests,
)
from llm_saturator import saturator_may_launch
from server.state import app_state


@pytest.fixture(autouse=True)
def _clean_pacer(monkeypatch, tmp_path):
    """Isolate the singleton from the real metrics file and reset state.

    Also baselines the shared app_state liveness fields via monkeypatch so a
    stale heartbeat or a leftover restart marker written by a test in THIS
    file cannot leak into later test files in the same session.
    """

    monkeypatch.setattr(
        llm_concurrency,
        "_QUOTA_METRICS_FILE_OVERRIDE",
        tmp_path / "nonexistent_metrics.jsonl",
    )
    monkeypatch.setattr(
        app_state, "_pipeline_heartbeat_monotonic", None, raising=False
    )
    app_state.clear_orchestrator_restart_pending()
    reset_quota_pacer_for_tests()
    monkeypatch.delenv("POK_LLM_QUOTA_WINDOW_BUDGET_TOKENS", raising=False)
    yield
    reset_quota_pacer_for_tests()
    app_state.clear_orchestrator_restart_pending()


def _enable_pacing(monkeypatch, budget: int, window_sec: float = 18000.0):
    monkeypatch.setenv("POK_LLM_QUOTA_WINDOW_BUDGET_TOKENS", str(budget))
    monkeypatch.setenv("POK_LLM_QUOTA_WINDOW_SEC", str(window_sec))


# --- unit: pacer math -------------------------------------------------------


def test_disabled_by_default_never_gates():
    pacer = QuotaPacer()
    pacer.note_usage(10_000_000_000, ts=time.time())
    assert pacer.opportunistic_blocked_scale() is None


def test_hourly_line_blocks_front_load(monkeypatch):
    # budget 10M / 18000s -> hourly line = 2M tokens.
    _enable_pacing(monkeypatch, 10_000_000)
    pacer = QuotaPacer()
    now = time.time()
    pacer.note_usage(1_999_999, ts=now)
    assert pacer.opportunistic_blocked_scale(now=now) is None
    # One more call crosses the trailing-1h line: the saturator lane parks.
    pacer.note_usage(50_000, ts=now)
    assert pacer.opportunistic_blocked_scale(now=now) == 3600.0


def test_window_line_binds_for_aged_burn(monkeypatch):
    # Burn older than 1h escapes the hourly line but not the window line.
    _enable_pacing(monkeypatch, 10_000_000)
    pacer = QuotaPacer()
    now = time.time()
    pacer.note_usage(9_500_000, ts=now - 7200.0)
    assert pacer.opportunistic_blocked_scale(now=now) is None
    pacer.note_usage(500_000, ts=now)
    assert pacer.opportunistic_blocked_scale(now=now) == 18000.0


def test_block_clears_as_events_age_out(monkeypatch):
    _enable_pacing(monkeypatch, 10_000_000)
    pacer = QuotaPacer()
    now = time.time()
    pacer.note_usage(2_500_000, ts=now)
    assert pacer.opportunistic_blocked_scale(now=now) == 3600.0
    # 90 minutes later the burst has left the trailing hour.
    assert pacer.opportunistic_blocked_scale(now=now + 5400.0) is None


def test_zero_token_calls_are_ignored():
    pacer = QuotaPacer()
    pacer.note_usage(0)
    pacer.note_usage(-5)
    pacer.note_usage("not-a-number")
    assert pacer.spend_in_window(3600.0) == 0


def test_snapshot_projection(monkeypatch):
    _enable_pacing(monkeypatch, 10_000_000)
    pacer = QuotaPacer()
    now = time.time()
    pacer.note_usage(1_000_000, ts=now)
    snap = pacer.snapshot(now=now)
    assert snap["budget_tokens"] == 10_000_000
    assert snap["window_sec"] == 18000.0
    assert snap["spend_1h_tokens"] == 1_000_000
    assert snap["spend_window_tokens"] == 1_000_000
    assert snap["opportunistic_blocked_scale_sec"] is None


# --- unit: restart seeding --------------------------------------------------


def test_seed_replays_only_preboot_inwindow_rows(monkeypatch, tmp_path):
    _enable_pacing(monkeypatch, 10_000_000)
    boot = time.time()
    rows = [
        # In-window, pre-boot: counted.
        {"epoch_ts": boot - 7200.0, "total_tokens": 9_500_000},
        # Too old for the 5h window: ignored.
        {"epoch_ts": boot - 6 * 3600.0, "total_tokens": 99_000_000},
        # Post-boot rows belong to the in-process hook: not double-booked.
        {"epoch_ts": boot + 10.0, "total_tokens": 7_000_000},
        # Malformed rows are skipped without failing the seed.
        {"epoch_ts": "garbage", "total_tokens": 1_000_000},
        {"epoch_ts": boot - 100.0},
        "not-json",
    ]
    metrics = tmp_path / "llm_call_metrics.jsonl"
    metrics.write_text(
        "".join(
            json.dumps(r) + "\n" if isinstance(r, dict) else r + "\n"
            for r in rows
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        llm_concurrency, "_QUOTA_METRICS_FILE_OVERRIDE", metrics
    )
    pacer = QuotaPacer(boot_ts=boot)
    # The gate path seeds lazily and must see the pre-boot window burn.
    blocked = pacer.opportunistic_blocked_scale(now=boot)
    assert pacer.spend_in_window(18000.0, now=boot) == 9_500_000
    # 9.5M of a 10M budget, all older than the hourly line: under both
    # lines (trailing-1h spend is 0), not blocked yet.
    assert blocked is None
    # In-process burn stacks on top of the seeded history and crosses the
    # WINDOW line (the hourly line alone would not bind for a 600k call).
    pacer.note_usage(600_000, ts=boot + 1.0)
    assert pacer.opportunistic_blocked_scale(now=boot + 2.0) == 18000.0


def test_seed_failure_is_fail_open(monkeypatch, tmp_path):
    _enable_pacing(monkeypatch, 10_000_000)
    broken = tmp_path / "broken.jsonl"
    broken.mkdir()  # a DIRECTORY at the metrics path: open() must fail softly
    monkeypatch.setattr(llm_concurrency, "_QUOTA_METRICS_FILE_OVERRIDE", broken)
    pacer = QuotaPacer()
    assert pacer.opportunistic_blocked_scale() is None  # no crash, no burn


# --- integration: hook + saturator gate -------------------------------------


def _record(**kwargs):
    """record_llm_call_metrics with the mandatory fields filled in."""

    defaults = dict(
        call_id="c1",
        attempt=0,
        role="SATURATOR",
        model="glm-5.3-flash",
        total_elapsed_sec=12.0,
    )
    defaults.update(kwargs)
    llm_call_metrics.record_llm_call_metrics(**defaults)


def test_metrics_hook_feeds_the_singleton(monkeypatch, tmp_path):
    _enable_pacing(monkeypatch, 10_000_000)
    metrics = tmp_path / "llm_call_metrics.jsonl"
    monkeypatch.setattr(llm_call_metrics, "_metrics_file", lambda: metrics)
    monkeypatch.setattr(
        llm_concurrency, "_QUOTA_METRICS_FILE_OVERRIDE", metrics
    )

    _record(call_id="c1", input_tokens=1_500_000, output_tokens=100_000)

    pacer = get_quota_pacer()
    assert pacer.spend_in_window(3600.0) == 1_600_000
    assert metrics.exists()  # the durable record still landed
    # note_llm_call_tokens routes to the same singleton.
    note_llm_call_tokens(400_001)
    assert pacer.spend_in_window(3600.0) == 2_000_001


def test_duplicate_record_counted_once_but_retry_attempts_count(
    monkeypatch, tmp_path
):
    """Dedupe is keyed on (call_id, attempt), never on call_id alone.

    llm_query_retry mints ONE billing_call_id per retry loop and reuses it
    for every signature-retry attempt — keying on call_id alone would
    silently drop the retry attempts' burn.
    """

    _enable_pacing(monkeypatch, 10_000_000)
    metrics = tmp_path / "llm_call_metrics.jsonl"
    monkeypatch.setattr(llm_call_metrics, "_metrics_file", lambda: metrics)
    monkeypatch.setattr(
        llm_concurrency, "_QUOTA_METRICS_FILE_OVERRIDE", metrics
    )

    # Same (call_id, attempt) recorded twice (a replayed/rewound write):
    # counted once.
    _record(call_id="call-x", attempt=0, input_tokens=100_000)
    _record(call_id="call-x", attempt=0, input_tokens=100_000)
    # Same call_id, NEXT attempt (a real signature retry): counted.
    _record(call_id="call-x", attempt=1, input_tokens=50_000)

    pacer = get_quota_pacer()
    assert pacer.spend_in_window(3600.0) == 150_000


def test_boot_race_row_is_counted_exactly_once(monkeypatch, tmp_path):
    """The lazy-import seam: a row written before this module's boot stamp
    exists both in the durable tail and in the hook's in-memory notification.

    The seed replays the file row; the hook must not double-book it. The
    epoch_ts == boot rounding edge (raw now straddling the stamp) resolves
    to the hook alone — also exactly once.
    """

    _enable_pacing(monkeypatch, 10_000_000)
    metrics = tmp_path / "llm_call_metrics.jsonl"
    monkeypatch.setattr(
        llm_concurrency, "_QUOTA_METRICS_FILE_OVERRIDE", metrics
    )

    # Row A: written BEFORE the pacer's boot stamp (the pre-import first
    # record). The seed counts it and registers its key.
    boot = time.time()
    metrics.write_text(
        json.dumps(
            {
                "epoch_ts": boot - 7200.0,
                "total_tokens": 3_000_000,
                "call_id": "race-a",
                "attempt": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    pacer = QuotaPacer(boot_ts=boot)
    pacer.opportunistic_blocked_scale(now=boot)  # triggers the lazy seed
    assert pacer.spend_in_window(18000.0, now=boot) == 3_000_000
    # The hook fires for that same record (the in-process notification):
    # the dedupe key must suppress the second count.
    pacer.note_usage(3_000_000, ts=boot - 7200.0, call_key=("race-a", 0))
    assert pacer.spend_in_window(18000.0, now=boot) == 3_000_000

    # Row B: epoch_ts exactly AT the boot stamp — excluded from the seed
    # (rounding edge), so the hook's notification is the only count.
    pacer_b = QuotaPacer(boot_ts=boot)
    metrics.write_text(
        json.dumps(
            {
                "epoch_ts": boot,
                "total_tokens": 2_000_000,
                "call_id": "race-b",
                "attempt": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    pacer_b.opportunistic_blocked_scale(now=boot)  # seeds from row B only
    assert pacer_b.spend_in_window(18000.0, now=boot) == 0
    pacer_b.note_usage(2_000_000, ts=boot - 0.0004, call_key=("race-b", 0))
    assert pacer_b.spend_in_window(18000.0, now=boot) == 2_000_000


def test_saturator_launch_refused_when_pace_line_crossed(monkeypatch):
    _enable_pacing(monkeypatch, 10_000_000)
    app_state.note_pipeline_heartbeat()  # pipeline alive: isolate the pacer
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert ok is True

    note_llm_call_tokens(2_000_000)  # exactly the hourly line
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "quota_pacing")

    # The block is dynamic: aging the burn out of the window reopens it.
    pacer = get_quota_pacer()
    assert pacer.opportunistic_blocked_scale(now=time.time() + 7200.0) is None


def test_pacer_gate_sits_below_pipeline_liveness_gate(monkeypatch):
    """A dead pipeline still parks (pipeline_not_alive outranks pacing)."""

    _enable_pacing(monkeypatch, 10_000_000)
    app_state._pipeline_heartbeat_monotonic = time.monotonic() - 700.0
    note_llm_call_tokens(9_000_000)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "pipeline_not_alive")


def test_restart_fill_lane_does_not_bypass_quota_pacing(monkeypatch):
    """The w3 bounded backoff fill stays inside the w2/w4/w5 pace clamp.

    A scheduled revival opens the fill lane, but the quota pacer still
    refuses the launch when a pace line is exhausted — the bounded lane may
    keep the provider warm, never re-front-load the 5h window.
    """

    _enable_pacing(monkeypatch, 10_000_000)
    app_state._pipeline_heartbeat_monotonic = time.monotonic() - 700.0
    app_state.note_orchestrator_restart_pending(30.0)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert ok is True and reason == "ok"

    note_llm_call_tokens(2_000_000)  # exactly the hourly line
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "quota_pacing")


# --- deploy: the production config actually enables the clamp ---------------


def test_committed_env_runtime_enables_pacing_with_kpi_clearing_values():
    """The w2/w4/w5 fix is a deploy decision as much as code: the committed
    cloud env must enable the pacer at a pace that (a) clears the 20M/h
    hourly KPI and (b) leaves headroom under the observed ~178M 5h cap."""

    env_path = (
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "tencent-cloud"
        / "env.runtime"
    )
    assert env_path.exists(), f"missing deploy env: {env_path}"
    values = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()

    budget = int(values["POK_LLM_QUOTA_WINDOW_BUDGET_TOKENS"])
    window = float(values["POK_LLM_QUOTA_WINDOW_SEC"])
    hourly = budget / window * 3600.0
    # (a) clears the 20M tokens/hour KPI ...
    assert hourly >= 20_000_000, hourly
    # (b) ... with headroom under the observed ~178M/5h provider capacity.
    assert budget <= 178_000_000, budget
    # The bounded restart-fill lane is part of the same committed config.
    assert int(values["POK_LLM_SATURATOR_RESTART_FILL_INFLIGHT"]) >= 1
