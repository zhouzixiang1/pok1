"""Block-40 (2026-11-02): cache-hit KPI caliber regression.

The GLM usage shape is OpenAI-compatible: ``input_tokens`` is the FULL
prompt and ``cached_input_tokens``/``cache_read_input_tokens`` is a SUBSET
of it (verified on 7976/7976 production raw_usage rows: cached <= input,
zero violations). The old KPI denominator added input to cache_read as if
the fields were disjoint (the Anthropic semantic), double-counting the
cached prefix and pinning the headline hit rate near 0.48 no matter the
real behavior (true weighted value 0.93).

Contract under test (both surfaces must agree):

* ``llm_call_metrics.record_llm_call_metrics`` writes ``cache_hit_rate``
  = cache_read / max(input, cache_read+cache_write).
* The ``/api/llm/metrics`` aggregate uses the SAME per-row denominator
  before summing.
* A single-shot call with no reuse is 0.0 (not None) when tokens exist;
  a zero-token row stays None.
"""

from __future__ import annotations

import json


def _record_row(tmp_path, monkeypatch, **kw) -> dict:
    import llm_call_metrics

    metrics_file = tmp_path / "llm_call_metrics.jsonl"
    monkeypatch.setattr(llm_call_metrics, "_metrics_file", lambda: metrics_file)
    kw.setdefault("call_id", "c1")
    kw.setdefault("attempt", 0)
    kw.setdefault("total_elapsed_sec", 1.0)
    llm_call_metrics.record_llm_call_metrics(**kw)
    return json.loads(metrics_file.read_text(encoding="utf-8").strip().splitlines()[-1])


def test_record_hit_rate_uses_subset_denominator(tmp_path, monkeypatch):
    # OpenAI-compat shape: full prompt 1000, of which 930 cached.
    row = _record_row(
        tmp_path,
        monkeypatch,
        role="SATURATOR STRATEGY RESEARCH",
        model="glm-5.3-flash",
        input_tokens=1000,
        output_tokens=50,
        cache_read_input_tokens=930,
        cache_creation_input_tokens=0,
        success=True,
    )
    # old caliber: 930/(930+0+1000) = 0.4819; fixed: 930/1000 = 0.93.
    assert row["cache_hit_rate"] == 0.93


def test_record_hit_rate_survives_anthropic_shaped_row(tmp_path, monkeypatch):
    # If a provider ever returns disjoint fields (input = uncached only),
    # max() keeps the denominator honest instead of under-reporting.
    row = _record_row(
        tmp_path,
        monkeypatch,
        role="MASTER_PLAN_AUDIT",
        model="x",
        input_tokens=100,
        output_tokens=10,
        cache_read_input_tokens=300,
        cache_creation_input_tokens=0,
        success=True,
    )
    assert row["cache_hit_rate"] == 1.0  # 300 / max(100, 300)


def test_record_zero_tokens_has_no_hit_rate(tmp_path, monkeypatch):
    row = _record_row(
        tmp_path,
        monkeypatch,
        role="X",
        model="x",
        input_tokens=0,
        output_tokens=0,
        success=False,
    )
    assert row["cache_hit_rate"] is None


def test_route_aggregate_denominator_matches_record(tmp_path, monkeypatch):
    from server.routes import llm_metrics as lm

    rows = [
        # OpenAI-compat full-prompt shape: 1000 total, 930 cached.
        {"success": True, "role": "R", "input_tokens": 1000,
         "cache_read_input_tokens": 930, "cache_creation_input_tokens": 0},
        # single-shot: no cached bytes at all.
        {"success": True, "role": "R", "input_tokens": 100,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    ]
    monkeypatch.setattr(lm, "_read_metrics_lines", lambda *a, **k: list(rows))
    summary = lm._compute_metrics_summary()
    agg = summary["roles"]["R"]
    # fixed caliber: 930 / (1000 + 100) = 0.8455; old: 930/2030 = 0.4581.
    assert agg["cache_hit_rate"] == 0.8455
