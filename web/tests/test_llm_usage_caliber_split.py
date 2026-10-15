"""B1 (2026-10-15): consumption-caliber split for /api/control/status.

During the 8h pipeline wedge the aggregate LLM token rate looked healthy
(40M+/h) because the background saturator alone kept it there; the role field
in ``llm_call_metrics.jsonl`` already distinguishes the two lanes (roles
prefixed ``SATURATOR``), but no projection surface exposed the split. The
control status now carries an additive ``llm_usage_by_caliber`` block with
per-window (1h/24h) ``pipeline_tokens`` / ``saturator_tokens`` /
``pipeline_calls`` computed by the streaming core summarizer
(``llm_call_metrics.summarize_llm_usage_by_caliber``).

Contract under test:

* Bucketing: role starting with ``SATURATOR`` (case-insensitive) is the
  saturator; everything else — including a missing or blank ``role`` — is the
  pipeline. ``*_calls`` counts JSONL rows (provider attempts), not unique
  ``call_id``s.
* Timestamps: ``epoch_ts`` first, then the ISO ``ts`` string; a row with
  neither inherits the nearest preceding anchored row's time (line-order
  approximation); a file with NO usable timestamp includes the whole file in
  every window with ``time_basis="line_order_unbounded"`` and a note.
* Tolerance: missing file, empty file, and malformed lines never raise.
* Backward compatibility: the status payload only gains the new key; the
  pre-existing top-level fields and Phase A blocks are unchanged, and a
  summarizer failure degrades to an explicitly unavailable block.
"""

from __future__ import annotations

import json
import time

import pytest

import llm_call_metrics
from llm_call_metrics import summarize_llm_usage_by_caliber


@pytest.fixture
def metrics(tmp_path, monkeypatch):
    """Point ``llm_call_metrics._metrics_file`` at a fresh tmp JSONL.

    Same pattern as the quota-pacer tests: the autouse ``isolate_state``
    fixture redirects ``evolution_infra.RESULTS_DIR`` elsewhere, so direct
    summarizer tests must patch the path resolver explicitly.
    """
    path = tmp_path / "llm_call_metrics.jsonl"
    monkeypatch.setattr(llm_call_metrics, "_metrics_file", lambda: path)
    return path


def _write_metrics_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _row(
    role,
    *,
    epoch_ts=None,
    ts=None,
    total_tokens=100,
    call_id="c1",
    attempt=0,
):
    row = {
        "schema_version": 1,
        "role": role,
        "call_id": call_id,
        "attempt": attempt,
    }
    if epoch_ts is not None:
        row["epoch_ts"] = epoch_ts
    if ts is not None:
        row["ts"] = ts
    if total_tokens is not None:
        row["total_tokens"] = total_tokens
    return row


def test_bucketing_by_role_prefix_and_time_window(metrics):
    now = 1_800_000_000.0
    _write_metrics_rows(metrics, [
        # Saturator, inside both windows.
        _row("SATURATOR STRATEGY RESEARCH", epoch_ts=now - 1800, total_tokens=1000),
        # Pipeline, inside both windows.
        _row("MASTER (Try 1)", epoch_ts=now - 1800, total_tokens=500),
        # Blank role still counts as pipeline.
        _row("", epoch_ts=now - 600, total_tokens=50),
        # Missing role key still counts as pipeline.
        {"schema_version": 1, "epoch_ts": now - 600, "total_tokens": 25},
        # Saturator, inside 24h only.
        _row("SATURATOR LINE AUDIT", epoch_ts=now - 2 * 3600, total_tokens=100),
        # Pipeline, outside both windows.
        _row("WORKER 1", epoch_ts=now - 30 * 3600, total_tokens=999),
        # Saturator, far outside both windows.
        _row("SATURATOR STRATEGY RESEARCH", epoch_ts=now - 40 * 3600, total_tokens=7),
    ])

    summary = summarize_llm_usage_by_caliber(now=now)

    assert summary["available"] is True
    assert summary["time_basis"] == "file_timestamps"
    assert summary["rows_scanned"] == 7
    assert summary["approximated_rows"] == 0
    assert summary["unplaced_rows"] == 0

    last_1h = summary["windows"]["last_1h"]
    assert last_1h == {
        "pipeline_tokens": 500 + 50 + 25,
        "saturator_tokens": 1000,
        "pipeline_calls": 3,
    }
    last_24h = summary["windows"]["last_24h"]
    assert last_24h == {
        "pipeline_tokens": 500 + 50 + 25,
        "saturator_tokens": 1000 + 100,
        "pipeline_calls": 3,
    }


def test_saturator_prefix_is_case_insensitive_and_trimmed(metrics):
    now = 1_800_000_000.0
    _write_metrics_rows(metrics, [
        _row("  saturator strategy research  ", epoch_ts=now - 60, total_tokens=10),
        # Merely containing the word is NOT a prefix.
        _row("MASTER SATURATOR REVIEW", epoch_ts=now - 60, total_tokens=20),
    ])
    summary = summarize_llm_usage_by_caliber(now=now)
    assert summary["windows"]["last_1h"] == {
        "pipeline_tokens": 20,
        "saturator_tokens": 10,
        "pipeline_calls": 1,
    }


def test_pipeline_calls_count_attempts_not_unique_call_ids(metrics):
    now = 1_800_000_000.0
    _write_metrics_rows(metrics, [
        _row("WORKER 1", epoch_ts=now - 60, total_tokens=10, call_id="same", attempt=0),
        _row("WORKER 1", epoch_ts=now - 50, total_tokens=10, call_id="same", attempt=1),
    ])
    summary = summarize_llm_usage_by_caliber(now=now)
    assert summary["windows"]["last_1h"]["pipeline_calls"] == 2


def test_iso_ts_string_used_when_epoch_ts_missing(metrics):
    now = 1_800_000_000.0
    from datetime import datetime, timezone

    iso_now = datetime.fromtimestamp(now, tz=timezone.utc)
    recent = datetime.fromtimestamp(now - 120, tz=timezone.utc).isoformat()
    old = datetime.fromtimestamp(now - 30 * 3600, tz=timezone.utc).isoformat()

    _write_metrics_rows(metrics, [
        _row("SATURATOR STRATEGY RESEARCH", ts=recent, total_tokens=400),
        _row("LEAD CODE REVIEWER", ts=old, total_tokens=300),
        _row("WORKER 1", ts=iso_now.isoformat(), total_tokens=5),
    ])
    summary = summarize_llm_usage_by_caliber(now=now)
    assert summary["time_basis"] == "file_timestamps"
    assert summary["windows"]["last_1h"] == {
        "pipeline_tokens": 5,
        "saturator_tokens": 400,
        "pipeline_calls": 1,
    }
    assert summary["windows"]["last_24h"]["saturator_tokens"] == 400
    assert summary["windows"]["last_24h"]["pipeline_tokens"] == 5


def test_row_without_timestamp_inherits_preceding_anchor_by_line_order(metrics):
    now = 1_800_000_000.0
    _write_metrics_rows(metrics, [
        _row("SATURATOR STRATEGY RESEARCH", epoch_ts=now - 120, total_tokens=100),
        # No time fields at all: inherits the preceding anchored timestamp.
        _row("WORKER 1", total_tokens=40),
        # Older anchor: the following unanchored row must inherit the OLDER
        # time (nearest preceding anchor), not the newest seen.
        _row("MASTER (Try 1)", epoch_ts=now - 30 * 3600, total_tokens=8),
        _row("WORKER 2", total_tokens=4),
    ])
    summary = summarize_llm_usage_by_caliber(now=now)
    assert summary["time_basis"] == "file_timestamps_with_line_order_approximation"
    assert summary["approximated_rows"] == 2
    assert summary["unplaced_rows"] == 0
    assert summary["windows"]["last_1h"] == {
        "pipeline_tokens": 40,
        "saturator_tokens": 100,
        "pipeline_calls": 1,
    }


def test_leading_rows_before_first_anchor_are_unplaced_not_counted(metrics):
    now = 1_800_000_000.0
    _write_metrics_rows(metrics, [
        _row("WORKER 1", total_tokens=500),
        _row("SATURATOR STRATEGY RESEARCH", epoch_ts=now - 60, total_tokens=100),
    ])
    summary = summarize_llm_usage_by_caliber(now=now)
    assert summary["unplaced_rows"] == 1
    assert summary["windows"]["last_1h"] == {
        "pipeline_tokens": 0,
        "saturator_tokens": 100,
        "pipeline_calls": 0,
    }


def test_all_rows_without_timestamps_include_whole_file_with_note(metrics):
    _write_metrics_rows(metrics, [
        _row("WORKER 1", total_tokens=300),
        _row("SATURATOR STRATEGY RESEARCH", total_tokens=2000),
        _row("MASTER (Try 1)", total_tokens=100),
    ])
    summary = summarize_llm_usage_by_caliber()
    assert summary["time_basis"] == "line_order_unbounded"
    assert summary["note"]
    assert "line-order" in summary["note"]
    for window in summary["windows"].values():
        assert window == {
            "pipeline_tokens": 400,
            "saturator_tokens": 2000,
            "pipeline_calls": 2,
        }


def test_missing_file_reports_unavailable_with_zeroed_windows(metrics):
    # Nothing written: the metrics file does not exist yet.
    summary = summarize_llm_usage_by_caliber(now=1_800_000_000.0)
    assert summary["available"] is False
    assert summary["reason"] == "metrics_file_unreadable"
    assert summary["time_basis"] == "unavailable"
    for window in summary["windows"].values():
        assert window == {
            "pipeline_tokens": 0,
            "saturator_tokens": 0,
            "pipeline_calls": 0,
        }


def test_empty_file_and_malformed_lines_are_tolerated(metrics):
    metrics.parent.mkdir(parents=True, exist_ok=True)
    now = 1_800_000_000.0
    metrics.write_text(
        "\n"
        "not json at all\n"
        + json.dumps(_row("WORKER 1", epoch_ts=now - 60, total_tokens=10)) + "\n"
        + json.dumps([1, 2, 3]) + "\n",  # valid JSON, not an object
        encoding="utf-8",
    )
    summary = summarize_llm_usage_by_caliber(now=now)
    assert summary["available"] is True
    assert summary["rows_scanned"] == 1
    assert summary["windows"]["last_1h"] == {
        "pipeline_tokens": 10,
        "saturator_tokens": 0,
        "pipeline_calls": 1,
    }

    # A genuinely empty (zero-byte) file is available with zeroed windows.
    metrics.write_text("", encoding="utf-8")
    summary = summarize_llm_usage_by_caliber(now=now)
    assert summary["available"] is True
    assert summary["time_basis"] == "no_rows"
    assert summary["rows_scanned"] == 0
    for window in summary["windows"].values():
        assert window == {
            "pipeline_tokens": 0,
            "saturator_tokens": 0,
            "pipeline_calls": 0,
        }


def test_total_tokens_falls_back_to_input_plus_output(metrics):
    now = 1_800_000_000.0
    _write_metrics_rows(metrics, [
        # Legacy/partial row: total_tokens absent, parts present.
        {
            "schema_version": 1,
            "role": "WORKER 1",
            "epoch_ts": now - 60,
            "input_tokens": 30,
            "output_tokens": 12,
        },
    ])
    summary = summarize_llm_usage_by_caliber(now=now)
    assert summary["windows"]["last_1h"]["pipeline_tokens"] == 42


def test_control_status_projection_is_additive_and_backward_compatible(client):
    """The status payload only gains ``llm_usage_by_caliber``.

    Every pre-existing top-level field (AppState fields, the epoch-overlay
    fields, the Phase A blocks, stability observation) must still be present
    with the new block attached — and the block itself must degrade to an
    explicit unavailable projection instead of breaking the endpoint.
    """
    import evolution_infra
    from server.routes import control

    now = time.time()
    metrics = evolution_infra.RESULTS_DIR / "llm_call_metrics.jsonl"
    _write_metrics_rows(metrics, [
        _row("SATURATOR STRATEGY RESEARCH", epoch_ts=now - 600, total_tokens=9_000_000),
        _row("MASTER (Try 1)", epoch_ts=now - 600, total_tokens=1_000),
    ])

    control._invalidate_observer_projection_cache()
    resp = client.get("/api/control/status")
    assert resp.status_code == 200
    data = resp.json()

    # Pre-existing fields the endpoint has always carried (AppState fields,
    # epoch overlay, Phase A blocks, stability observation) are unchanged.
    for field in (
        "running",
        "mode",
        "daemon_enabled",
        "current_v",
        "next_v",
        "generation_count",
        "epoch_state",
        "active_generations",
        "pipeline_mode",
        "feature_flags",
        "version_authority",
        "eval_wait",
        "stability_observation",
        "stability_observation_digest",
        "post_publication_handoff",
    ):
        assert field in data, f"pre-existing status field missing: {field}"

    block = data["llm_usage_by_caliber"]
    assert block["available"] is True
    assert set(block["windows"]) == {"last_1h", "last_24h"}
    for window in block["windows"].values():
        assert set(window) == {
            "pipeline_tokens",
            "saturator_tokens",
            "pipeline_calls",
        }
    assert block["windows"]["last_1h"]["saturator_tokens"] == 9_000_000
    assert block["windows"]["last_1h"]["pipeline_tokens"] == 1_000
    assert block["windows"]["last_1h"]["pipeline_calls"] == 1
    assert block["windows"]["last_24h"]["saturator_tokens"] == 9_000_000


def test_control_status_survives_summarizer_failure(client, monkeypatch):
    """A broken projection degrades to an explicit unavailable block.

    The endpoint must still answer 200 with every field above intact — the
    overlay is fail-closed and purely additive.
    """
    from server.routes import control

    def _boom():
        raise RuntimeError("metrics scan exploded")

    monkeypatch.setattr(control, "_llm_usage_by_caliber_projection", _boom)
    control._invalidate_observer_projection_cache()
    resp = client.get("/api/control/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["llm_usage_by_caliber"]["available"] is False
    assert data["llm_usage_by_caliber"]["time_basis"] == "unavailable"
    for window in data["llm_usage_by_caliber"]["windows"].values():
        assert window == {
            "pipeline_tokens": 0,
            "saturator_tokens": 0,
            "pipeline_calls": 0,
        }
    assert "running" in data
    assert "stability_observation" in data


def test_control_projection_helper_shape_and_mtime_cache(tmp_path, monkeypatch):
    """The control-side builder returns the core summary mtime-cached."""
    from server.cache import clear_mtime_cache
    from server.routes import control

    metrics_path = tmp_path / "llm_call_metrics.jsonl"
    monkeypatch.setattr(llm_call_metrics, "_metrics_file", lambda: metrics_path)
    clear_mtime_cache()

    # Missing file -> unavailable (not cached as None; producer re-invoked).
    summary = control._llm_usage_by_caliber_projection()
    assert summary["available"] is False

    now = time.time()
    _write_metrics_rows(metrics_path, [
        _row("SATURATOR STRATEGY RESEARCH", epoch_ts=now - 60, total_tokens=5),
        _row("WORKER 1", epoch_ts=now - 60, total_tokens=2),
    ])
    summary = control._llm_usage_by_caliber_projection()
    assert summary["available"] is True
    assert summary["windows"]["last_1h"] == {
        "pipeline_tokens": 2,
        "saturator_tokens": 5,
        "pipeline_calls": 1,
    }
