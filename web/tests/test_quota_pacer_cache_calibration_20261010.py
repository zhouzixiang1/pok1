"""QuotaPacer cache-dimension calibration (2026-10-10 red-team residual).

The burn ledger recorded only successful calls' ``total_tokens``
(input+output, no cache). The provider's 5h usage unit is unknown — the
observed ~178M 1308 ceiling and the 150M committed budget are both
no-cache calibers, so the two are not comparable until the ledger tracks
both sides.

Contract:

* (a) ``POK_LLM_QUOTA_CACHE_WEIGHT`` (default 0.0) weights
  cache_read+cache_creation tokens into the WINDOW consumption the pace
  lines consume. Default 0.0 is exactly the historical behavior: gate,
  ``spend_in_window`` and every existing snapshot field are unchanged.
* (b) Every ``pipeline.llm_quota_exceeded_detected`` emission site
  (llm_query_retry, both the outer handler and the signature-retry loop)
  attaches ``pacer_window_spend`` — the ledger's with-cache and
  without-cache dual-caliber window snapshot — so the next real 1308 can
  calibrate the provider unit.
* (c) Failed attempts: a metrics row that carries usage fields — including
  ``success=False`` rows written by ``_record_failed_attempt_metrics`` and
  any ``attempt > 0`` row — is accounted by BOTH the restart seed and the
  in-process ``note_usage`` hook, identically to a success row.
"""

from __future__ import annotations

import inspect
import json
import time
from types import SimpleNamespace

import pytest

import llm_call_metrics
import llm_concurrency
import llm_query_retry
from llm_concurrency import (
    QuotaPacer,
    get_quota_pacer,
    note_llm_call_tokens,
    quota_pacer_dual_caliber_snapshot,
    reset_quota_pacer_for_tests,
)


@pytest.fixture(autouse=True)
def _clean_pacer(monkeypatch, tmp_path):
    monkeypatch.setattr(
        llm_concurrency,
        "_QUOTA_METRICS_FILE_OVERRIDE",
        tmp_path / "nonexistent_metrics.jsonl",
    )
    reset_quota_pacer_for_tests()
    monkeypatch.delenv("POK_LLM_QUOTA_WINDOW_BUDGET_TOKENS", raising=False)
    monkeypatch.delenv("POK_LLM_QUOTA_CACHE_WEIGHT", raising=False)
    yield
    reset_quota_pacer_for_tests()


def _enable_pacing(monkeypatch, budget: int, window_sec: float = 18000.0):
    monkeypatch.setenv("POK_LLM_QUOTA_WINDOW_BUDGET_TOKENS", str(budget))
    monkeypatch.setenv("POK_LLM_QUOTA_WINDOW_SEC", str(window_sec))


# --- (a) cache dimension ------------------------------------------------------


def test_default_weight_is_exactly_the_historical_behavior(monkeypatch):
    _enable_pacing(monkeypatch, 10_000_000)
    assert llm_concurrency._quota_cache_weight() == 0.0
    pacer = QuotaPacer()
    now = time.time()
    pacer.note_usage(1_000_000, ts=now, cache_tokens=5_000_000)
    # Base caliber ignores cache entirely.
    assert pacer.spend_in_window(3600.0, now=now) == 1_000_000
    # Weighted caliber at weight 0.0 is numerically identical.
    assert pacer.spend_weighted_in_window(3600.0, now=now) == 1_000_000.0
    # The pace line does not see the cache tokens: 1M of the 2M hourly line.
    assert pacer.opportunistic_blocked_scale(now=now) is None


def test_weighted_gate_counts_cache_at_configured_weight(monkeypatch):
    _enable_pacing(monkeypatch, 10_000_000)  # hourly line = 2M
    monkeypatch.setenv("POK_LLM_QUOTA_CACHE_WEIGHT", "0.5")
    assert llm_concurrency._quota_cache_weight() == 0.5
    pacer = QuotaPacer()
    now = time.time()
    # Base burn 1.8M (under the line); cache 400k at weight 0.5 adds 200k
    # -> weighted 2.0M == the hourly line -> blocked.
    pacer.note_usage(1_800_000, ts=now, cache_tokens=400_000)
    assert pacer.spend_in_window(3600.0, now=now) == 1_800_000
    assert pacer.spend_weighted_in_window(3600.0, now=now) == 2_000_000.0
    assert pacer.opportunistic_blocked_scale(now=now) == 3600.0

    # Same events, default weight: not blocked (regression guard for (a)).
    monkeypatch.delenv("POK_LLM_QUOTA_CACHE_WEIGHT")
    pacer2 = QuotaPacer()
    pacer2.note_usage(1_800_000, ts=now, cache_tokens=400_000)
    assert pacer2.opportunistic_blocked_scale(now=now) is None


def test_invalid_weight_falls_back_to_zero(monkeypatch):
    monkeypatch.setenv("POK_LLM_QUOTA_CACHE_WEIGHT", "not-a-float")
    assert llm_concurrency._quota_cache_weight() == 0.0
    monkeypatch.setenv("POK_LLM_QUOTA_CACHE_WEIGHT", "-2")
    assert llm_concurrency._quota_cache_weight() == 0.0


def test_snapshot_carries_both_calibers(monkeypatch):
    _enable_pacing(monkeypatch, 10_000_000)
    monkeypatch.setenv("POK_LLM_QUOTA_CACHE_WEIGHT", "1.0")
    pacer = QuotaPacer()
    now = time.time()
    pacer.note_usage(1_000_000, ts=now, cache_tokens=250_000)
    snap = pacer.snapshot(now=now)
    # Existing fields keep the base caliber (back-compat).
    assert snap["spend_1h_tokens"] == 1_000_000
    assert snap["spend_window_tokens"] == 1_000_000
    assert snap["opportunistic_blocked_scale_sec"] is None
    # New cache-dimension fields.
    assert snap["cache_weight"] == 1.0
    assert snap["spend_1h_tokens_with_cache"] == 1_250_000
    assert snap["spend_window_tokens_with_cache"] == 1_250_000


def test_cache_only_event_is_recorded_when_weight_positive(monkeypatch):
    pacer = QuotaPacer()
    now = time.time()
    pacer.note_usage(0, ts=now, cache_tokens=700_000)
    assert pacer.spend_in_window(3600.0, now=now) == 0
    monkeypatch.setenv("POK_LLM_QUOTA_CACHE_WEIGHT", "1.0")
    assert pacer.spend_weighted_in_window(3600.0, now=now) == 700_000.0
    # Zero-everything events stay ignored, as before.
    pacer.note_usage(0, ts=now, cache_tokens=0)
    assert pacer.spend_weighted_in_window(3600.0, now=now) == 700_000.0


def test_metrics_hook_forwards_cache_dimension(monkeypatch, tmp_path):
    _enable_pacing(monkeypatch, 10_000_000)
    monkeypatch.setenv("POK_LLM_QUOTA_CACHE_WEIGHT", "1.0")
    metrics = tmp_path / "llm_call_metrics.jsonl"
    monkeypatch.setattr(llm_call_metrics, "_metrics_file", lambda: metrics)
    monkeypatch.setattr(
        llm_concurrency, "_QUOTA_METRICS_FILE_OVERRIDE", metrics
    )

    llm_call_metrics.record_llm_call_metrics(
        call_id="c-cache",
        attempt=0,
        role="SATURATOR",
        model="glm-5.3-flash",
        total_elapsed_sec=10.0,
        input_tokens=1_500_000,
        output_tokens=100_000,
        cache_read_input_tokens=2_000_000,
        cache_creation_input_tokens=3,
    )

    pacer = get_quota_pacer()
    assert pacer.spend_in_window(3600.0) == 1_600_000  # base unchanged
    assert pacer.spend_weighted_in_window(3600.0) == 3_600_003.0


# --- (b) dual-caliber snapshot on the 1308 event ------------------------------


def test_dual_caliber_snapshot_shape(monkeypatch):
    _enable_pacing(monkeypatch, 10_000_000)
    monkeypatch.setenv("POK_LLM_QUOTA_CACHE_WEIGHT", "0.25")
    note_llm_call_tokens(1_000_000, cache_tokens=800_000)
    snap = quota_pacer_dual_caliber_snapshot()
    assert snap is not None
    assert snap["budget_tokens"] == 10_000_000
    assert snap["window_sec"] == 18000.0
    assert snap["cache_weight"] == 0.25
    assert snap["spend_1h_tokens_without_cache"] == 1_000_000
    assert snap["spend_window_tokens_without_cache"] == 1_000_000
    assert snap["spend_1h_tokens_with_cache"] == 1_200_000  # +0.25*800k
    assert snap["spend_window_tokens_with_cache"] == 1_200_000


def test_both_1308_emission_sites_carry_the_dual_caliber_field():
    """(b) source-level wiring: both pipeline.llm_quota_exceeded_detected
    emitters attach pacer_window_spend from quota_pacer_dual_caliber_snapshot."""
    source = inspect.getsource(llm_query_retry)
    assert source.count("pipeline.llm_quota_exceeded_detected") == 2
    assert source.count("pacer_window_spend=") == 2
    assert (
        source.count("quota_pacer_dual_caliber_snapshot(") == 2
    )
    # The module-level accessor exists and is fail-soft.
    snap = quota_pacer_dual_caliber_snapshot()
    assert isinstance(snap, dict) and "cache_weight" in snap


# --- (c) failed attempts enter the ledger -------------------------------------


def _seed_rows(monkeypatch, tmp_path, boot, rows):
    metrics = tmp_path / "llm_call_metrics.jsonl"
    metrics.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    monkeypatch.setattr(
        llm_concurrency, "_QUOTA_METRICS_FILE_OVERRIDE", metrics
    )
    return QuotaPacer(boot_ts=boot)


def test_seed_accounts_failure_rows_and_attempt_gt_zero_rows(
    monkeypatch, tmp_path
):
    """(c) the restart seed has no success/attempt filter: a success=False
    row and attempt>0 rows with usage are replayed into the ledger."""
    _enable_pacing(monkeypatch, 10_000_000)
    monkeypatch.setenv("POK_LLM_QUOTA_CACHE_WEIGHT", "1.0")
    boot = time.time()
    pacer = _seed_rows(
        monkeypatch,
        tmp_path,
        boot,
        [
            # attempt>0 success row (a signature retry that landed).
            {
                "epoch_ts": boot - 7200.0,
                "total_tokens": 400_000,
                "attempt": 1,
                "call_id": "retry-call",
            },
            # FAILED attempt row with usage (new success=False shape).
            {
                "epoch_ts": boot - 3600.0,
                "total_tokens": 600_000,
                "cache_read_input_tokens": 100_000,
                "attempt": 2,
                "call_id": "retry-call",
                "success": False,
                "error_type": "LLMRoleTimeout",
            },
        ],
    )
    pacer.opportunistic_blocked_scale(now=boot)  # triggers the lazy seed
    assert pacer.spend_in_window(18000.0, now=boot) == 1_000_000
    assert pacer.spend_weighted_in_window(18000.0, now=boot) == 1_100_000.0
    # The in-process hook does not double-book the seeded rows.
    pacer.note_usage(
        400_000, ts=boot - 7200.0, call_key=("retry-call", 1)
    )
    assert pacer.spend_in_window(18000.0, now=boot) == 1_000_000


def _failed_attempt_row(**kwargs):
    """Drive _record_failed_attempt_metrics with one observable result."""
    defaults = dict(
        role_name="SATURATOR",
        billing_call_id="failed-call",
        attempt=1,
        billing_results=[
            SimpleNamespace(
                total_cost_usd=0.25,
                usage={
                    "input_tokens": 900_000,
                    "output_tokens": 50_000,
                    "cache_read_input_tokens": 40_000,
                },
            )
        ],
        attempt_started_at=time.time() - 30.0,
        metrics_recorded=False,
        error=RuntimeError("stream died after the result"),
        model="glm-5.3-flash",
    )
    defaults.update(kwargs)
    llm_query_retry._record_failed_attempt_metrics(**defaults)


def test_failed_attempt_with_usage_writes_row_and_feeds_pacer(
    monkeypatch, tmp_path
):
    _enable_pacing(monkeypatch, 10_000_000)
    monkeypatch.setenv("POK_LLM_QUOTA_CACHE_WEIGHT", "1.0")
    metrics = tmp_path / "llm_call_metrics.jsonl"
    monkeypatch.setattr(llm_call_metrics, "_metrics_file", lambda: metrics)
    monkeypatch.setattr(
        llm_concurrency, "_QUOTA_METRICS_FILE_OVERRIDE", metrics
    )

    _failed_attempt_row()

    rows = [json.loads(line) for line in metrics.read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["success"] is False
    assert row["attempt"] == 1
    assert row["error_type"] == "RuntimeError"
    assert row["input_tokens"] == 900_000
    assert row["cache_read_input_tokens"] == 40_000
    # The pacer hook inside record_llm_call_metrics accounted it: base
    # 950k, weighted 990k with the cache dimension.
    pacer = get_quota_pacer()
    assert pacer.spend_in_window(3600.0) == 950_000
    assert pacer.spend_weighted_in_window(3600.0) == 990_000.0


def test_failed_attempt_recording_is_suppressed_correctly(monkeypatch, tmp_path):
    _enable_pacing(monkeypatch, 10_000_000)
    metrics = tmp_path / "llm_call_metrics.jsonl"
    monkeypatch.setattr(llm_call_metrics, "_metrics_file", lambda: metrics)

    result = SimpleNamespace(
        total_cost_usd=0.1,
        usage={"input_tokens": 5, "output_tokens": 5},
    )
    # Success path already recorded the row.
    _failed_attempt_row(
        metrics_recorded=True, billing_results=[result]
    )
    # No exception in flight at scope exit.
    _failed_attempt_row(error=None, billing_results=[result])
    # No observable ResultMessage (transport died before any result).
    _failed_attempt_row(billing_results=[])
    # Result present but carries NO usage fields.
    _failed_attempt_row(
        billing_results=[SimpleNamespace(total_cost_usd=0.1, usage=None)]
    )
    assert not metrics.exists()
    assert QuotaPacer().spend_in_window(3600.0) == 0


def test_retry_loop_finally_wires_failure_recording():
    """Source-level wiring: the retry loop's finally passes the attempt's
    billing scope into _record_failed_attempt_metrics before resetting it."""
    source = inspect.getsource(
        llm_query_retry._run_stream_with_signature_retry_attempts
    )
    assert "_record_failed_attempt_metrics(" in source
    assert "metrics_recorded=_attempt_metrics_recorded" in source
    assert "error=sys.exc_info()[1]" in source
