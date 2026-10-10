"""disk hygiene must not race in-flight atomic checkpoint publishes (w6).

2026-10-10 05:59:32: ``_reap_tmp_and_stale_locks`` unlinked the checkpoint
writer's in-flight ``.pipeline_state.json.<hex>.tmp`` (created O_EXCL,
written+fsynced, then ``os.replace``d by
``evolution_infra_state_io._atomic_publish_state_text``) because the
``.tmp`` branch ignored the ``stale`` age gate the lock branch already
applied. The writer's ``os.replace`` raised ENOENT, the orchestrator
crashed, and a ``precommit_failed`` checkpoint write was lost — the only
orchestrator crash since 10-08, forcing a redundant third 49-minute
precommit round.

Contract (fixed 2026-10-10):

* A FRESH ``.tmp`` (mtime within ``max_age_sec``, default 3600s) in the
  results root is left alone — it is presumed to be an in-flight atomic
  publish (checkpoint state, hygiene's own jsonl trims, any tmp+rename
  writer).
* A STALE ``.tmp`` (older than ``max_age_sec``) is still reaped — leftover
  garbage from a crashed writer must not accumulate forever.
* ``.hygiene.tmp`` leftovers follow the same policy.
* The ``.json.lock`` stale-reap behavior is unchanged.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import disk_hygiene


def _make_results(tmp_path: Path) -> Path:
    results = tmp_path / "results"
    results.mkdir(parents=True, exist_ok=True)
    return results


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def test_fresh_checkpoint_tmp_survives_reap(tmp_path: Path):
    """The exact w6 race: an in-flight atomic-publish tmp is not deleted."""

    results = _make_results(tmp_path)
    # Exact pattern produced by _atomic_publish_state_text:
    # f".{path.name}.{uuid.uuid4().hex}.tmp"
    in_flight = results / ".pipeline_state.json.8f14e45fceea167a5a36dedd4bea2543.tmp"
    in_flight.write_text('{"stage": "critic_checked", "partial":', encoding="utf-8")

    freed, removed = disk_hygiene._reap_tmp_and_stale_locks(results)

    assert in_flight.exists()
    assert removed == 0
    assert freed == 0


def test_stale_checkpoint_tmp_is_still_reaped(tmp_path: Path):
    """Leftover tmp from a crashed writer (>1h) must not accumulate."""

    results = _make_results(tmp_path)
    leftover = results / ".pipeline_state.json.deadbeef.tmp"
    leftover.write_text("{}", encoding="utf-8")
    _age(leftover, 7200.0)

    freed, removed = disk_hygiene._reap_tmp_and_stale_locks(results)

    assert not leftover.exists()
    assert removed == 1


def test_fresh_and_stale_hygiene_tmp_follow_same_policy(tmp_path: Path):
    results = _make_results(tmp_path)
    fresh = results / "llm_call_metrics.jsonl.hygiene.tmp"
    fresh.write_bytes(b"x" * 32)
    stale = results / "events.jsonl.hygiene.tmp"
    stale.write_bytes(b"y" * 32)
    _age(stale, 4000.0)

    disk_hygiene._reap_tmp_and_stale_locks(results)

    assert fresh.exists()
    assert not stale.exists()


def test_stale_lock_reap_behavior_unchanged(tmp_path: Path):
    """The pre-existing stale-gated .json.lock branch is not regressed."""

    results = _make_results(tmp_path)
    # A lock whose json name is PROTECTED is never reaped, even stale —
    # protection covers the lock sidecar too (unchanged behavior).
    protected_lock = results / "llm_availability_pause.json.lock"
    protected_lock.write_text("", encoding="utf-8")
    _age(protected_lock, 7200.0)
    # A stale orphan lock for a non-protected json is reaped...
    orphan_lock = results / "rating_cycle_x.json.lock"
    orphan_lock.write_text("", encoding="utf-8")
    _age(orphan_lock, 7200.0)
    # ...but a FRESH orphan lock is left alone (same age-gate policy).
    fresh_orphan_lock = results / "rating_cycle_y.json.lock"
    fresh_orphan_lock.write_text("", encoding="utf-8")

    disk_hygiene._reap_tmp_and_stale_locks(results)

    assert protected_lock.exists()
    assert not orphan_lock.exists()
    assert fresh_orphan_lock.exists()


def test_full_hygiene_pass_spares_in_flight_checkpoint_tmp(tmp_path: Path):
    """End-to-end through run_disk_hygiene: the janitor pass that raced the
    writer on 05:59:32 (freed=21.4MB in the same minute) leaves a fresh
    checkpoint tmp alone while still reaping stale garbage."""

    results = _make_results(tmp_path)
    # Live checkpoint + a fresh in-flight publish beside it.
    (results / "pipeline_state.json").write_text(
        json.dumps({"next_v": 535, "source_v": 534, "stage": "precommit_running"}),
        encoding="utf-8",
    )
    in_flight = results / ".pipeline_state.json.abc123.tmp"
    in_flight.write_text('{"partial":', encoding="utf-8")
    # Plus unrelated stale tmp garbage that SHOULD go.
    stale = results / "old_cache.json.feed.tmp"
    stale.write_bytes(b"z" * 64)
    _age(stale, 5000.0)

    report = disk_hygiene.run_disk_hygiene(results, min_free_bytes=1 << 40)

    assert report["ok"] is True
    assert in_flight.exists()
    assert not stale.exists()
