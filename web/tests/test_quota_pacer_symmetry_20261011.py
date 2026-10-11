"""QuotaPacer hook/seed accounting symmetry (2026-10-11).

Two asymmetries fixed here (both only bite when
``POK_LLM_QUOTA_CACHE_WEIGHT > 0``, but both are accounting-correctness bugs
in their own right):

(a) **Cache-only rows.** ``record_llm_call_metrics`` writes a usage row that
carries ONLY cache fields as ``"total_tokens": None`` (``total_tok or None``)
while its hook (``note_llm_call_tokens(0, cache_tokens=...)``) DOES account
it — ``note_usage`` records ``(ts, 0, cache)``. The restart seed
(``_read_metrics_tail_events``) used to require ``total_tokens`` to be a
positive int and dropped exactly those rows, so a restart silently lost the
cache-only burn the in-process hook had counted. The seed now admits a row
when EITHER leg carries tokens, mirroring ``note_usage`` admission.

(b) **Signature-retry continue path.** The retry loop's ``finally`` passed
``error=sys.exc_info()[1]`` into ``_record_failed_attempt_metrics``. When the
signature-retry branch leaves the except handler via ``continue`` (py3
semantics: the handled exception is cleared once the except block is exited,
including via ``continue``), ``sys.exc_info()[1]`` is ``None`` in the
finally, so the guard ``if error is None: return`` skipped the failure row —
an attempt that had already produced usage (observable ResultMessages in the
billing scope) and then hit the signature error was burned provider-side but
never entered the ledger when the loop RETRIED (only the propagate path
recorded it). The retry branch now stashes the in-flight error and the
finally consumes ``sys.exc_info()[1] or _attempt_pending_error``.

The headline assertion for (a): the in-process hook and the restart seed,
given the SAME ``llm_call_metrics.jsonl`` (written by the real recorder),
derive the SAME weighted window spend at cache weight > 0.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ClaudeSDKError

import llm_call_metrics
import llm_concurrency
import llm_query as _lq
import llm_query_retry
from llm_concurrency import QuotaPacer, get_quota_pacer, reset_quota_pacer_for_tests


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
    monkeypatch.delenv("POK_LLM_TRANSPORT", raising=False)
    yield
    reset_quota_pacer_for_tests()


# --- (a) hook vs seed on one metrics file --------------------------------------


def _record_usage_rows(metrics):
    """Two rows through the REAL recorder: one full row, one cache-only."""
    llm_call_metrics.record_llm_call_metrics(
        call_id="sym-full",
        attempt=0,
        role="SATURATOR",
        model="glm-5.3-flash",
        total_elapsed_sec=12.0,
        input_tokens=500_000,
        output_tokens=100_000,
        cache_read_input_tokens=200_000,
        cache_creation_input_tokens=0,
    )
    llm_call_metrics.record_llm_call_metrics(
        call_id="sym-cache-only",
        attempt=0,
        role="SATURATOR",
        model="glm-5.3-flash",
        total_elapsed_sec=12.0,
        # input/output None: the recorder computes total_tok=0 and stores
        # "total_tokens": None — the exact cache-only row shape.
        input_tokens=None,
        output_tokens=None,
        cache_read_input_tokens=250_000,
        cache_creation_input_tokens=50_000,
    )
    rows = [json.loads(line) for line in metrics.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["total_tokens"] == 600_000
    assert rows[1]["total_tokens"] is None  # cache-only row shape on disk
    return rows


def test_hook_and_seed_agree_on_the_same_metrics_file_20261011(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("POK_LLM_QUOTA_CACHE_WEIGHT", "1.0")
    metrics = tmp_path / "llm_call_metrics.jsonl"
    monkeypatch.setattr(llm_call_metrics, "_metrics_file", lambda: metrics)
    # BOTH paths read the same file: the hook writes it, the seed replays it.
    monkeypatch.setattr(llm_concurrency, "_QUOTA_METRICS_FILE_OVERRIDE", metrics)

    _record_usage_rows(metrics)

    # Hook path: the singleton counted both rows in memory (seed saw no
    # pre-boot rows; the rows are newer than module load).
    hook_pacer = get_quota_pacer()
    now = time.time()
    base = hook_pacer.spend_in_window(18000.0, now=now)
    weighted = hook_pacer.spend_weighted_in_window(18000.0, now=now)
    assert base == 600_000  # cache-only contributes 0 to the base caliber
    assert weighted == 600_000 + 200_000 + 300_000

    # Seed path: a fresh process restarted NOW replays the same file.
    # (_ensure_seeded directly: budget 0 legitimately disables the gate, and
    # the gate's early return must not gate the seed for this test.)
    seeded = QuotaPacer(boot_ts=now + 10.0)
    seeded._ensure_seeded()
    assert seeded.spend_in_window(18000.0, now=now) == base
    assert seeded.spend_weighted_in_window(18000.0, now=now) == weighted


def test_seed_skips_fully_empty_rows_exactly_like_the_hook_20261011(tmp_path):
    pacer = QuotaPacer(boot_ts=time.time() + 10.0)
    rows = [
        # No usage at all — ignored by both calibers on both paths.
        {"epoch_ts": time.time() - 60.0, "call_id": "empty", "attempt": 0},
        # Cache-only with zero cache — also ignored (zero-everything).
        {
            "epoch_ts": time.time() - 60.0,
            "call_id": "zero-cache",
            "attempt": 0,
            "cache_read_input_tokens": 0,
        },
    ]
    metrics = tmp_path / "llm_call_metrics.jsonl"
    metrics.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    import llm_concurrency as lc

    original = lc._QUOTA_METRICS_FILE_OVERRIDE
    lc._QUOTA_METRICS_FILE_OVERRIDE = metrics
    try:
        pacer.opportunistic_blocked_scale(now=time.time())
    finally:
        lc._QUOTA_METRICS_FILE_OVERRIDE = original
    assert pacer.spend_in_window(18000.0, now=time.time()) == 0


# --- (b) signature-retry continue path -----------------------------------------


def test_retry_continue_path_records_usage_bearing_attempt_20261011(
    monkeypatch, tmp_path
):
    """A signature error AFTER usage was produced, retried (not raised),
    must still write its success=False row and feed the pacer."""
    monkeypatch.setenv("POK_LLM_QUOTA_WINDOW_BUDGET_TOKENS", "100000000")
    monkeypatch.setenv("POK_LLM_QUOTA_CACHE_WEIGHT", "1.0")
    metrics = tmp_path / "llm_call_metrics.jsonl"
    monkeypatch.setattr(llm_call_metrics, "_metrics_file", lambda: metrics)
    monkeypatch.setattr(
        llm_concurrency, "_QUOTA_METRICS_FILE_OVERRIDE", metrics
    )

    # --- transport plumbing: keep the loop on the claude path, no probes ---
    monkeypatch.setattr(_lq, "_assert_no_unresolved_provider_attempts", lambda: None)
    monkeypatch.setattr(_lq, "_new_owned_sdk_transport", lambda *_a, **_k: object())
    monkeypatch.setattr(_lq, "_new_provider_attempt", lambda *_a, **_k: None)
    monkeypatch.setattr(
        _lq, "_cleanup_owned_provider_attempt", lambda *a, **k: _noop_async()
    )
    monkeypatch.setattr(
        llm_query_retry, "_record_completed_billing_attempt",
        lambda **_k: (0.0, None),
    )
    monkeypatch.setattr(
        llm_query_retry, "_signature_retry_sleep", lambda *a, **k: _noop_async()
    )
    monkeypatch.setattr(_lq, "claude_query", lambda **_k: object())

    async def _noop_async():
        return None

    attempts = {"n": 0}

    async def fake_process_stream(_gen, _log, _ui, _role):
        attempts["n"] += 1
        if attempts["n"] == 1:
            # The attempt already produced a ResultMessage with usage, THEN
            # the stream died with the signature error.
            results = _lq._LLM_BILLING_RESULTS.get()
            results.append(
                SimpleNamespace(
                    total_cost_usd=0.2,
                    usage={
                        "input_tokens": 900_000,
                        "output_tokens": 50_000,
                        "cache_read_input_tokens": 40_000,
                    },
                )
            )
            raise ClaudeSDKError("signature: missing required field foo")
        return (["recovered text"], 0.05, {"input_tokens": 200_000}, {})

    monkeypatch.setattr(_lq, "_process_stream", fake_process_stream)

    texts, cost, usage = asyncio.run(
        llm_query_retry._run_stream_with_signature_retry_attempts(
            "prompt", SimpleNamespace(model="glm-5.3-flash"), None, None,
            "TEST-SYM",
        )
    )
    assert texts == ["recovered text"]
    assert attempts["n"] == 2

    rows = [json.loads(line) for line in metrics.read_text().splitlines()]
    assert len(rows) == 2
    failed = next(r for r in rows if r["success"] is False)
    succeeded = next(r for r in rows if r["success"] is True)
    # The retried (continue-path) attempt's usage survived into a row.
    assert failed["attempt"] == 0
    assert failed["error_type"] == "ClaudeSDKError"
    assert failed["input_tokens"] == 900_000
    assert failed["cache_read_input_tokens"] == 40_000
    assert succeeded["attempt"] == 1
    assert succeeded["input_tokens"] == 200_000

    # Both attempts feed the burn ledger through the hook.
    pacer = get_quota_pacer()
    now = time.time()
    assert pacer.spend_in_window(18000.0, now=now) == (
        900_000 + 50_000 + 200_000
    )
    assert pacer.spend_weighted_in_window(18000.0, now=now) == (
        1_150_000 + 40_000
    )

    # And a restart seeding from the same file agrees (both rows replay).
    seeded = QuotaPacer(boot_ts=now + 10.0)
    seeded._ensure_seeded()
    assert seeded.spend_weighted_in_window(18000.0, now=now) == (
        pacer.spend_weighted_in_window(18000.0, now=now)
    )


def test_retry_loop_wires_pending_error_into_failure_recording_20261011():
    """Source-level wiring guard for (b): the stash and its consumption."""
    import inspect

    source = inspect.getsource(
        llm_query_retry._run_stream_with_signature_retry_attempts
    )
    assert "_attempt_pending_error = None" in source
    assert "_attempt_pending_error = e" in source
    assert "error=sys.exc_info()[1] or _attempt_pending_error" in source
