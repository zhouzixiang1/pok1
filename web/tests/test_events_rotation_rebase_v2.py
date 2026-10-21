"""events.jsonl rotation rebase v2: gzip re-head + watermark v3 rebase.

Pins the frozen janitor contract of ``evolution_infra_rotation_rebase``:

* digest-proven re-head (gzip batch -> atomic replace -> v3 baseline
  watermark -> completed rebase state), zero event-byte loss at every
  crash breakpoint (review B1: prepared-with-grown-live recovers by
  hash, not by size equality);
* recovery reachability independent of every env gate (review B2: a
  wedged v2-watermark-over-shrunk-live source heals at the plan-build
  entry point);
* R1: the v3 baseline watermark's ``previous_watermark_digest`` binds
  the COMPLETED rebase-state digest and stays bound after the ⑥->⑦
  crash window converges;
* R2: the superseded watermark object is preserved in full;
* R3: ``verify_rebase_chain`` consumes both digests of every gzip batch;
* retention never deletes the live watermark's / pending handoff's /
  current rebase-state's batches and sweeps its own archive-dir temps
  (R4).
"""

from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import evolution_infra
import evolution_infra_rotation_rebase as rb
from bot_artifact import canonical_digest

_PUBLICATION_ID = "b" * 64
_EVENTS = "events.jsonl"


def _lines(count: int, start: int = 0) -> bytes:
    return b"".join(
        json.dumps({"row": row}).encode("utf-8") + b"\n"
        for row in range(start, start + count)
    )


def _rotate(version: int):
    plan = evolution_infra.build_archive_rotation_plan(version, _PUBLICATION_ID)
    receipts = evolution_infra.archive_rotate_files(version, plan)
    return plan, receipts


def _events_receipt(receipts) -> dict:
    return next(item for item in receipts if item["source"] == _EVENTS)


def _age(path: Path, days: float) -> None:
    stamp = time.time() - days * 86400.0
    os.utime(path, (stamp, stamp))


@pytest.fixture
def rebase_runtime(tmp_path, monkeypatch):
    results = tmp_path / "results"
    archive = results / "archive"
    results.mkdir()
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "ARCHIVE_DIR", archive)
    monkeypatch.setattr(
        evolution_infra,
        "POST_PUBLICATION_HANDOFF_DIR",
        results / "post_publication_handoffs",
    )
    for name in (
        "WORKER_FAILURES_FILE",
        "MATCH_HISTORY_FILE",
        "RATING_HISTORY_FILE",
        "LLM_COSTS_FILE",
    ):
        monkeypatch.setattr(
            evolution_infra, name, results / f"{name.lower()}.jsonl"
        )
    source = results / _EVENTS
    return SimpleNamespace(
        results=results, archive=archive, source=source
    )


def _live_watermark(source: Path) -> dict:
    return json.loads(
        (rb.rebase_state_path(source).parent / f"{source.stem}.rotation-watermark.json")
        .read_text(encoding="utf-8")
    )


def test_rebase_reheads_gzips_and_advances_to_v3_then_back_to_v2(
    rebase_runtime,
):
    source = rebase_runtime.source
    source.write_bytes(_lines(1200))
    original = source.read_bytes()
    _plan, receipts = _rotate(1)
    end1 = _events_receipt(receipts)["end_offset"]
    v2_watermark = _live_watermark(source)
    assert v2_watermark["kind"] == "append-log-archive-watermark-v2"

    outcome = rb.try_rebase_rotation_source(
        source, min_cold_bytes=16, max_span_bytes=len(original)
    )
    assert outcome["ok"] is True and outcome["status"] == "rebased"

    state = rb.load_rebase_state(source)
    assert state["state"] == "completed"
    drop = state["drop_offset"]
    assert original[drop - 1:drop] == b"\n"
    assert source.read_bytes() == original[drop:]
    assert state["old_size"] == len(original)
    assert type(state["pinned_ino"]) is int and state["pinned_ino"] > 0
    # R2: full superseded watermark object preserved, not just its digest.
    assert state["superseded_watermark"] == v2_watermark
    assert state["superseded_watermark_digest"] == v2_watermark["digest"]

    gz_path, receipt_path = rb.gzip_batch_paths(source, state["rebase_id"])
    assert gz_path.is_file() and receipt_path.is_file()
    with gzip.open(gz_path, "rb") as handle:
        assert handle.read() == original[end1:drop]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["range_start"] == end1
    assert receipt["range_end"] == drop
    assert receipt["previous_watermark_digest"] == v2_watermark["digest"]

    watermark = _live_watermark(source)
    assert watermark["kind"] == "append-log-archive-watermark-v3"
    assert watermark["end_offset"] == 0
    assert watermark["last_version"] is None
    assert watermark["previous_watermark_digest"] == state["digest"]

    # v3 loads through the shared loader and reboots planning at offset 0.
    loaded = evolution_infra._load_rotation_watermark(
        source, source.read_bytes()
    )
    assert loaded == watermark
    assert rb.verify_rebase_chain(source)["ok"] is True

    # After the next publication rotation the watermark returns to v2,
    # chained onto the v3 baseline digest.
    with source.open("ab") as handle:
        handle.write(_lines(1100, start=1200))
    _plan2, receipts2 = _rotate(2)
    events2 = _events_receipt(receipts2)
    assert events2["start_offset"] == 0
    watermark2 = _live_watermark(source)
    assert watermark2["kind"] == "append-log-archive-watermark-v2"
    assert watermark2["previous_watermark_digest"] == watermark["digest"]


def test_watermark_v3_loader_rejects_forged_variants(rebase_runtime):
    source = rebase_runtime.source
    source.write_bytes(_lines(1200))
    _plan, _receipts = _rotate(1)
    assert rb.try_rebase_rotation_source(
        source, min_cold_bytes=16, max_span_bytes=8 << 20
    )["status"] == "rebased"
    watermark_path = (
        rb.rebase_state_path(source).parent
        / f"{source.stem}.rotation-watermark.json"
    )
    good = json.loads(watermark_path.read_text(encoding="utf-8"))

    def _resign(mutate) -> None:
        forged = dict(good)
        mutate(forged)
        forged.pop("digest")
        forged["digest"] = canonical_digest(forged)
        watermark_path.write_text(
            json.dumps(forged), encoding="utf-8"
        )

    _resign(lambda record: record.update(end_offset=5))
    with pytest.raises(RuntimeError):
        evolution_infra._load_rotation_watermark(
            source, source.read_bytes()
        )
    _resign(lambda record: record.update(last_version=1))
    with pytest.raises(RuntimeError):
        evolution_infra._load_rotation_watermark(
            source, source.read_bytes()
        )
    _resign(lambda record: record.update(previous_watermark_digest="0" * 64))
    with pytest.raises(RuntimeError):
        evolution_infra._load_rotation_watermark(
            source, source.read_bytes()
        )
    _resign(lambda record: record.update(end_offset=0))
    loaded = evolution_infra._load_rotation_watermark(
        source, source.read_bytes()
    )
    assert loaded["kind"] == "append-log-archive-watermark-v3"


def test_crash_after_prepared_with_grown_live_recovers_zero_loss(
    rebase_runtime, monkeypatch,
):
    source = rebase_runtime.source
    source.write_bytes(_lines(1200))
    original = source.read_bytes()
    _plan, receipts = _rotate(1)
    end1 = _events_receipt(receipts)["end_offset"]

    real_replace = rb._os_replace
    calls = {"n": 0}

    def crash_before_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected pre-replace crash")
        return real_replace(src, dst)

    monkeypatch.setattr(rb, "_os_replace", crash_before_replace)
    with pytest.raises(RuntimeError, match="pre-replace"):
        rb.try_rebase_rotation_source(
            source, min_cold_bytes=16, max_span_bytes=len(original)
        )
    state = rb.load_rebase_state(source)
    assert state["state"] == "prepared"
    assert source.read_bytes() == original  # re-head not committed yet

    # Writers resume appending while the janitor is dead (review B1).
    growth = _lines(50, start=1200)
    with source.open("ab") as handle:
        handle.write(growth)
    grown = original + growth
    assert len(grown) != state["old_size"]  # size equality must NOT be required

    monkeypatch.setattr(rb, "_os_replace", real_replace)
    outcome = rb.recover_pending_rebase(source)
    assert outcome["ok"] is True and outcome["status"] == "recovered"

    recovered_state = rb.load_rebase_state(source)
    assert recovered_state["state"] == "completed"
    drop = recovered_state["drop_offset"]
    assert source.read_bytes() == grown[drop:]  # zero loss incl. delta
    assert recovered_state["kept_size"] == len(grown) - drop

    watermark = _live_watermark(source)
    assert watermark["kind"] == "append-log-archive-watermark-v3"
    assert watermark["previous_watermark_digest"] == recovered_state["digest"]
    assert rb.verify_rebase_chain(source)["ok"] is True
    gz_path, _receipt = rb.gzip_batch_paths(
        source, recovered_state["rebase_id"]
    )
    with gzip.open(gz_path, "rb") as handle:
        assert handle.read() == original[end1:drop]


def test_crash_after_replace_before_v3_unwedges_via_plan_entry(
    rebase_runtime, monkeypatch,
):
    source = rebase_runtime.source
    source.write_bytes(_lines(1200))
    original = source.read_bytes()
    _plan, receipts = _rotate(1)
    end1 = _events_receipt(receipts)["end_offset"]

    real_replace = rb._os_replace
    calls = {"n": 0}

    def replace_then_crash(src, dst):
        calls["n"] += 1
        result = real_replace(src, dst)
        if calls["n"] == 1:
            raise RuntimeError("injected post-replace crash")
        return result

    monkeypatch.setattr(rb, "_os_replace", replace_then_crash)
    with pytest.raises(RuntimeError, match="post-replace"):
        rb.try_rebase_rotation_source(
            source,
            min_cold_bytes=16,
            # keep a tail smaller than end1 so the stale v2 watermark end
            # provably exceeds the re-headed live length (the B2 wedge)
            max_span_bytes=len(original) - end1 - 200,
        )
    state = rb.load_rebase_state(source)
    assert state["state"] == "prepared"
    assert len(source.read_bytes()) < end1  # wedge precondition
    with pytest.raises(RuntimeError):
        evolution_infra._load_rotation_watermark(
            source, source.read_bytes()
        )

    monkeypatch.setattr(rb, "_os_replace", real_replace)
    # B2: the plan-build entry point heals the wedge itself -- recovery
    # must not depend on the janitor cycle or any env gate.
    plan2 = evolution_infra.build_archive_rotation_plan(2, _PUBLICATION_ID)
    events_snapshot = next(
        item
        for item in plan2["source_snapshots"]
        if item["source"] == _EVENTS
    )
    assert events_snapshot["watermark_end_offset"] == 0
    assert rb.load_rebase_state(source)["state"] == "completed"
    watermark = _live_watermark(source)
    assert watermark["kind"] == "append-log-archive-watermark-v3"


def test_crash_between_v3_and_completed_keeps_pwd_bound_to_completed_digest(
    rebase_runtime, monkeypatch,
):
    source = rebase_runtime.source
    source.write_bytes(_lines(1200))
    _plan, _receipts = _rotate(1)

    real_write = evolution_infra._write_rotation_record
    failed = {"n": 0}

    def fail_completed_state_once(path, payload, *, kind, keys):
        if (
            payload.get("kind") == rb.REBASE_STATE_KIND
            and payload.get("state") == "completed"
            and failed["n"] == 0
        ):
            failed["n"] += 1
            raise OSError("injected completed-state crash")
        return real_write(path, payload, kind=kind, keys=keys)

    monkeypatch.setattr(
        evolution_infra, "_write_rotation_record", fail_completed_state_once
    )
    with pytest.raises(OSError, match="completed-state"):
        rb.try_rebase_rotation_source(
            source, min_cold_bytes=16, max_span_bytes=8 << 20
        )
    prepared = rb.load_rebase_state(source)
    assert prepared["state"] == "prepared"
    watermark = _live_watermark(source)
    assert watermark["kind"] == "append-log-archive-watermark-v3"

    outcome = rb.recover_pending_rebase(source)
    assert outcome["ok"] is True and outcome["status"] == "recovered"
    completed = rb.load_rebase_state(source)
    assert completed["state"] == "completed"
    # R1: pwd stays bound to the COMPLETED record digest and the
    # completed record re-derives to exactly that digest.
    assert watermark["previous_watermark_digest"] == completed["digest"]
    assert completed["completed_at"] == prepared["completed_at"]
    reloaded = evolution_infra._load_rotation_watermark(
        source, source.read_bytes()
    )
    assert reloaded == _live_watermark(source)


def test_window_check_pending_before_heavy_io_skips(rebase_runtime, monkeypatch):
    source = rebase_runtime.source
    source.write_bytes(_lines(1200))
    _plan, _receipts = _rotate(1)
    monkeypatch.setattr(
        rb,
        "_pending_handoff_route",
        lambda: {"status": "pending", "version": 9, "records": []},
    )
    outcome = rb.try_rebase_rotation_source(
        source, min_cold_bytes=16, max_span_bytes=8 << 20
    )
    assert outcome["ok"] is False
    assert outcome["status"] == "skipped"
    assert outcome["reason"] == "handoff_active"
    assert rb.load_rebase_state(source) is None
    assert not list(rebase_runtime.archive.glob("events.rebase-*.jsonl.gz"))
    assert _live_watermark(source)["kind"] == "append-log-archive-watermark-v2"


def test_window_recheck_inside_ex_aborts_without_prepared(
    rebase_runtime, monkeypatch,
):
    source = rebase_runtime.source
    source.write_bytes(_lines(1200))
    original = source.read_bytes()
    _plan, _receipts = _rotate(1)
    calls = {"n": 0}

    def none_then_pending():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "none", "records": []}
        return {"status": "pending", "version": 9, "records": []}

    monkeypatch.setattr(rb, "_pending_handoff_route", none_then_pending)
    outcome = rb.try_rebase_rotation_source(
        source, min_cold_bytes=16, max_span_bytes=8 << 20
    )
    assert outcome["ok"] is False
    assert outcome["reason"] == "handoff_active"
    # The re-head must NOT be committed and no prepared intent survives.
    assert rb.load_rebase_state(source) is None
    assert source.read_bytes() == original
    assert _live_watermark(source)["kind"] == "append-log-archive-watermark-v2"
    # The EX critical section was released: appends work again.
    with source.open("ab") as handle:
        handle.write(b'{"row": 1200}\n')


def test_retention_protects_watermark_and_pending_versions(
    rebase_runtime, monkeypatch,
):
    archive = rebase_runtime.archive
    source = rebase_runtime.source
    source.write_bytes(_lines(1200))
    _rotate(1)
    with source.open("ab") as handle:
        handle.write(_lines(300, start=1200))
    _rotate(2)
    with source.open("ab") as handle:
        handle.write(_lines(300, start=1500))
    _rotate(3)
    assert _live_watermark(source)["last_version"] == 3

    for version in (1, 2, 3):
        for suffix in ("jsonl", "rotation.json"):
            _age(archive / f"events_v{version}.{suffix}", 40)

    monkeypatch.setattr(
        rb,
        "_pending_handoff_route",
        lambda: {"status": "pending", "version": 2, "records": []},
    )
    outcome = rb.apply_archive_batch_retention(
        source, retention_days=30.0, min_interval_sec=0.0
    )
    assert outcome["ok"] is True
    assert outcome["status"] == "applied"
    assert not (archive / "events_v1.jsonl").exists()
    assert not (archive / "events_v1.rotation.json").exists()
    assert (archive / "events_v2.jsonl").exists()  # pending handoff version
    assert (archive / "events_v3.jsonl").exists()  # live watermark version
    for version in (1, 2, 3):
        assert (archive / f"rotation-set-v{version}.plan.json").exists()

    ledger = rb.archive_retention_ledger_path(source)
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    removed = {row["name"] for row in rows}
    assert removed == {"events_v1.jsonl", "events_v1.rotation.json"}
    assert all(row["kind"] == "append-log-archive-retention-v1" for row in rows)
    assert outcome["receipt_path"] == str(ledger)

    monkeypatch.setattr(
        rb, "_pending_handoff_route", lambda: {"status": "none", "records": []}
    )
    second = rb.apply_archive_batch_retention(
        source, retention_days=30.0, min_interval_sec=0.0
    )
    assert second["ok"] is True
    assert not (archive / "events_v2.jsonl").exists()
    assert (archive / "events_v3.jsonl").exists()  # still watermark-protected


def test_retention_protects_rebase_state_batch_and_sweeps_tmp(
    rebase_runtime, monkeypatch,
):
    archive = rebase_runtime.archive
    source = rebase_runtime.source
    source.write_bytes(_lines(1200))
    _plan, _receipts = _rotate(1)
    assert rb.try_rebase_rotation_source(
        source, min_cold_bytes=16, max_span_bytes=8 << 20
    )["status"] == "rebased"
    state = rb.load_rebase_state(source)
    gz_path, receipt_path = rb.gzip_batch_paths(source, state["rebase_id"])
    orphan_gz = (
        archive / f"{source.stem}.rebase-{'f' * 16}.jsonl.gz"
    )
    orphan_gz.write_bytes(b"orphan")
    stale_tmp = archive / f"{source.stem}.rebase-{'e' * 16}.jsonl.gz.tmp"
    stale_tmp.write_bytes(b"stale")
    fresh_tmp = archive / f"{source.stem}.rebase-{'d' * 16}.jsonl.gz.tmp"
    fresh_tmp.write_bytes(b"fresh")
    for path in (gz_path, receipt_path, orphan_gz, stale_tmp):
        _age(path, 40)

    outcome = rb.apply_archive_batch_retention(
        source, retention_days=30.0, min_interval_sec=0.0
    )
    assert outcome["ok"] is True
    # Current completed rebase-state's own batch stays; orphan gzip and
    # stale module temp (>1h, R4) go; a fresh in-flight temp survives.
    assert gz_path.is_file() and receipt_path.is_file()
    assert rb.load_rebase_state(source)["state"] == "completed"
    assert not orphan_gz.exists()
    assert not stale_tmp.exists()
    assert fresh_tmp.exists()
    assert "events.rebase-" in " ".join(outcome["removed"])


def test_verify_rebase_chain_consumes_both_digests(rebase_runtime):
    source = rebase_runtime.source
    source.write_bytes(_lines(1200))
    _plan, receipts = _rotate(1)
    end1 = _events_receipt(receipts)["end_offset"]
    assert rb.try_rebase_rotation_source(
        source, min_cold_bytes=16, max_span_bytes=8 << 20
    )["status"] == "rebased"
    state = rb.load_rebase_state(source)
    gz_path, receipt_path = rb.gzip_batch_paths(source, state["rebase_id"])
    drop = state["drop_offset"]

    # Corrupt the gzip payload itself -> gzip_sha256 mismatch.
    raw = gz_path.read_bytes()
    gz_path.write_bytes(raw + b"\x00")
    report = rb.verify_rebase_chain(source)
    assert report["ok"] is False
    gz_path.write_bytes(raw)
    assert rb.verify_rebase_chain(source)["ok"] is True

    # Re-sign the receipt with a forged plaintext digest: the record
    # digest still verifies; the chain consumer catches the forgery
    # against the state's dual-digest batch entry.
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["plaintext_sha256"] = "0" * 64
    receipt.pop("digest")
    receipt["digest"] = canonical_digest(receipt)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    report = rb.verify_rebase_chain(source)
    assert report["ok"] is False
    assert "gzip_receipt_binding_mismatch" in report["issues"]


def test_try_rebase_skip_reason_tokens(rebase_runtime, monkeypatch):
    source = rebase_runtime.source
    source.write_bytes(_lines(1200))
    _plan, _receipts = _rotate(1)

    disabled = rb.try_rebase_rotation_source(
        source, enabled=False, min_cold_bytes=16
    )
    assert disabled["ok"] is False
    assert disabled["status"] == "skipped"
    assert disabled["reason"] == "disabled"

    below = rb.try_rebase_rotation_source(
        source, min_cold_bytes=10 ** 9
    )
    assert below["status"] == "skipped"
    assert below["reason"] == "below_threshold"

    wrong = rb.try_rebase_rotation_source(
        rebase_runtime.results / "worker_failures.jsonl", min_cold_bytes=16
    )
    assert wrong["reason"] == "not_rebase_source"

    other_dir = rb.try_rebase_rotation_source(
        source, results_dir=rebase_runtime.results / "elsewhere"
    )
    assert other_dir["reason"] == "not_rebase_source"

    missing = rb.try_rebase_rotation_source(
        rebase_runtime.results / "missing" / _EVENTS, min_cold_bytes=16
    )
    assert missing["reason"] == "source_missing"

    # The disk gate with a switchable seam first (before any completed
    # rebase state exists in this ARCHIVE_DIR, which is keyed by stem),
    # then a successful rebase, then the interval gate on the retry.
    free_override = {"value": None}
    real_free = rb._disk_free_bytes

    def switchable_free(path):
        if free_override["value"] is None:
            return real_free(path)
        return free_override["value"]

    monkeypatch.setattr(rb, "_disk_free_bytes", switchable_free)
    free_override["value"] = 0
    disk = rb.try_rebase_rotation_source(source, min_cold_bytes=16)
    assert disk["reason"] == "insufficient_disk"
    free_override["value"] = None

    assert rb.try_rebase_rotation_source(
        source, min_cold_bytes=16, max_span_bytes=8 << 20
    )["status"] == "rebased"
    interval = rb.try_rebase_rotation_source(
        source, min_cold_bytes=16, min_interval_sec=3600.0
    )
    assert interval["reason"] == "interval"


def test_locked_reader_sees_only_reheaded_tail(rebase_runtime):
    source = rebase_runtime.source
    source.write_bytes(_lines(1200))
    original = source.read_bytes()
    _plan, _receipts = _rotate(1)
    outcome = rb.try_rebase_rotation_source(
        source,
        min_cold_bytes=16,
        max_span_bytes=max(1, len(original) - 400),
    )
    assert outcome["status"] == "rebased"
    state = rb.load_rebase_state(source)
    with evolution_infra.locked_file(source, "r") as handle:
        tail = handle.read()
    assert tail.encode("utf-8") == original[state["drop_offset"]:]
