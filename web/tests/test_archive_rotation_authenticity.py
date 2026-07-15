"""Authenticity, crash recovery and read-only checks for JSONL cold copies."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import evolution_infra
from bot_artifact import canonical_digest

_PUBLICATION_ID = "a" * 64


def _rotation_plan(version: int):
    return evolution_infra.build_archive_rotation_plan(
        version,
        _PUBLICATION_ID,
    )


@pytest.fixture
def rotation_runtime(tmp_path, monkeypatch):
    results = tmp_path / "results"
    archive = results / "archive"
    results.mkdir()
    paths = {
        "WORKER_FAILURES_FILE": results / "worker_failures.jsonl",
        "MATCH_HISTORY_FILE": results / "match_history.jsonl",
        "RATING_HISTORY_FILE": results / "rating_history.jsonl",
        "LLM_COSTS_FILE": results / "llm_costs.jsonl",
    }
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "ARCHIVE_DIR", archive)
    for name, path in paths.items():
        monkeypatch.setattr(evolution_infra, name, path)
    source = paths["WORKER_FAILURES_FILE"]
    source.write_bytes(b"".join(
        json.dumps({"row": row}).encode("utf-8") + b"\n"
        for row in range(205)
    ))
    return results, archive, source


def _tree_bytes(root: Path):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_rotation_is_append_only_chained_and_purely_revalidatable(
    rotation_runtime,
):
    _results, archive, source = rotation_runtime
    original = source.read_bytes()

    rotation_plan = _rotation_plan(143)
    receipts = evolution_infra.archive_rotate_files(143, rotation_plan)

    assert len(receipts) == 1
    assert receipts[0]["source"] == source.name
    assert receipts[0]["source_preserved_append_only"] is True
    assert source.read_bytes() == original
    plan = json.loads(
        (archive / "worker_failures_v143.rotation.json").read_text()
    )
    watermark = json.loads(
        (archive / "worker_failures.rotation-watermark.json").read_text()
    )
    assert plan["schema_version"] == 2
    assert plan["kind"] == "append-log-nondestructive-rotation-v2"
    assert plan["state"] == "completed"
    assert watermark["last_plan_digest"] == plan["digest"]
    assert watermark["last_rotation_id"] == plan["rotation_id"]
    before = _tree_bytes(archive.parent)

    assert evolution_infra.validate_archive_rotation_receipts(
        143,
        receipts,
        rotation_plan=rotation_plan,
    ) == receipts
    assert _tree_bytes(archive.parent) == before


def test_rotation_recovers_archive_before_watermark_crash(
    rotation_runtime,
    monkeypatch,
):
    _results, archive, _source = rotation_runtime
    real_write = evolution_infra._write_rotation_record
    failed = False

    def fail_watermark_once(path, payload, *, kind, keys):
        nonlocal failed
        if kind == evolution_infra._ROTATION_WATERMARK_KIND and not failed:
            failed = True
            raise OSError("injected watermark crash")
        return real_write(path, payload, kind=kind, keys=keys)

    monkeypatch.setattr(
        evolution_infra,
        "_write_rotation_record",
        fail_watermark_once,
    )
    rotation_plan = _rotation_plan(143)
    with pytest.raises(OSError, match="injected watermark crash"):
        evolution_infra.archive_rotate_files(143, rotation_plan)
    assert (archive / "worker_failures_v143.jsonl").is_file()
    assert json.loads(
        (archive / "worker_failures_v143.rotation.json").read_text()
    )["state"] == "completed"

    receipts = evolution_infra.archive_rotate_files(143, rotation_plan)
    assert evolution_infra.validate_archive_rotation_receipts(
        143, receipts, rotation_plan=rotation_plan
    )


def test_rotation_rejects_resigned_field_drift_and_links(
    rotation_runtime,
):
    _results, archive, _source = rotation_runtime
    rotation_plan = _rotation_plan(143)
    receipts = evolution_infra.archive_rotate_files(143, rotation_plan)
    plan_path = archive / "worker_failures_v143.rotation.json"
    plan = json.loads(plan_path.read_text())
    plan["forged_extra"] = True
    unsigned = {key: value for key, value in plan.items() if key != "digest"}
    plan["digest"] = canonical_digest(unsigned)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(RuntimeError, match="fields mismatch"):
        evolution_infra.validate_archive_rotation_receipts(
            143, receipts, rotation_plan=rotation_plan
        )

    # Restore by recreating the isolated fixture's valid record, then prove a
    # second hard link is rejected by the no-follow/single-link reader.
    plan.pop("forged_extra")
    unsigned = {key: value for key, value in plan.items() if key != "digest"}
    plan["digest"] = canonical_digest(unsigned)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    extra = archive / "plan-hardlink"
    os.link(plan_path, extra)
    with pytest.raises(OSError, match="opened safe regular file"):
        evolution_infra.validate_archive_rotation_receipts(
            143, receipts, rotation_plan=rotation_plan
        )


def test_rotation_reproof_requires_receipt_for_every_frozen_plan(
    rotation_runtime,
):
    _results, _archive, _source = rotation_runtime
    rotation_plan = _rotation_plan(143)
    receipts = evolution_infra.archive_rotate_files(143, rotation_plan)
    assert len(receipts) == 1

    with pytest.raises(RuntimeError, match="receipt missing: worker_failures.jsonl"):
        evolution_infra.validate_archive_rotation_receipts(
            143, [], rotation_plan=rotation_plan
        )


def test_high_level_plan_rejects_empty_before_low_level_plan_exists(
    rotation_runtime,
):
    _results, archive, _source = rotation_runtime
    rotation_plan = _rotation_plan(143)

    assert [
        item["source"] for item in rotation_plan["expected_rotations"]
    ] == ["worker_failures.jsonl"]
    authority = archive / "rotation-set-v143.plan.json"
    assert json.loads(authority.read_text(encoding="utf-8")) == rotation_plan
    assert not (archive / "worker_failures_v143.rotation.json").exists()
    assert _rotation_plan(143) == rotation_plan
    with pytest.raises(
        RuntimeError,
        match="receipt missing: worker_failures.jsonl",
    ):
        evolution_infra.validate_archive_rotation_receipts(
            143,
            [],
            rotation_plan=rotation_plan,
        )
    assert not (archive / "worker_failures_v143.rotation.json").exists()


def test_high_level_plan_authority_recovers_publish_then_raise(
    rotation_runtime,
    monkeypatch,
):
    _results, archive, _source = rotation_runtime
    real_publish = evolution_infra._atomic_publish_state_text
    failed = False

    def publish_then_raise(path, raw):
        nonlocal failed
        real_publish(path, raw)
        if path.name == "rotation-set-v143.plan.json" and not failed:
            failed = True
            raise OSError("injected authority acknowledgement crash")

    monkeypatch.setattr(
        evolution_infra,
        "_atomic_publish_state_text",
        publish_then_raise,
    )
    with pytest.raises(OSError, match="acknowledgement crash"):
        _rotation_plan(143)
    assert (archive / "rotation-set-v143.plan.json").is_file()

    recovered = _rotation_plan(143)
    assert recovered["expected_rotations"][0]["source"] == (
        "worker_failures.jsonl"
    )
    assert not (archive / "worker_failures_v143.rotation.json").exists()


def test_rotation_executor_converges_to_frozen_range_after_live_append(
    rotation_runtime,
):
    _results, archive, source = rotation_runtime
    rotation_plan = _rotation_plan(143)
    expected = rotation_plan["expected_rotations"][0]
    frozen_end = expected["end_offset"]
    frozen_archive = source.read_bytes()[:frozen_end]
    with source.open("ab") as handle:
        handle.write(b"".join(
            json.dumps({"later": row}).encode("utf-8") + b"\n"
            for row in range(500)
        ))
    live = source.read_bytes()

    receipts = evolution_infra.archive_rotate_files(143, rotation_plan)

    assert receipts == evolution_infra.expected_archive_rotation_receipts(
        rotation_plan,
        version=143,
        publication_id=_PUBLICATION_ID,
    )
    assert receipts[0]["end_offset"] == frozen_end
    assert (archive / "worker_failures_v143.jsonl").read_bytes() == frozen_archive
    assert source.read_bytes() == live


def test_resigned_empty_high_level_plan_cannot_hide_frozen_cold_range(
    rotation_runtime,
):
    _results, _archive, _source = rotation_runtime
    rotation_plan = json.loads(json.dumps(_rotation_plan(143)))
    worker = rotation_plan["source_snapshots"][0]
    worker["snapshot_exists"] = True
    worker["snapshot_size"] = 0
    worker["snapshot_sha256"] = evolution_infra._rotation_digest(b"")
    worker["cold_end_offset"] = worker["watermark_end_offset"]
    worker["expected_rotation"] = None
    rotation_plan["expected_rotations"] = []
    rotation_plan["source_snapshot_set_digest"] = canonical_digest(
        rotation_plan["source_snapshots"]
    )
    rotation_plan["expected_rotation_set_digest"] = canonical_digest([])
    unsigned = {
        key: value
        for key, value in rotation_plan.items()
        if key != "authority_digest"
    }
    rotation_plan["authority_digest"] = canonical_digest(unsigned)

    with pytest.raises(RuntimeError, match="authority mismatch"):
        evolution_infra.validate_archive_rotation_receipts(
            143,
            [],
            rotation_plan=rotation_plan,
        )


def test_planless_empty_rotation_reproof_creates_no_sidecars(rotation_runtime):
    results, archive, source = rotation_runtime
    source.write_text('{"row": 1}\n', encoding="utf-8")
    rotation_plan = _rotation_plan(143)
    before = _tree_bytes(results)

    assert evolution_infra.validate_archive_rotation_receipts(
        143, [], rotation_plan=rotation_plan
    ) == []
    assert _tree_bytes(results) == before
    assert not any(archive.glob("*.rotation.json"))


def test_later_rotation_extends_chain_without_invalidating_prior_receipt(
    rotation_runtime,
):
    _results, _archive, source = rotation_runtime
    first_plan = _rotation_plan(143)
    first = evolution_infra.archive_rotate_files(143, first_plan)
    with source.open("ab") as handle:
        handle.write(b"".join(
            json.dumps({"row": row}).encode("utf-8") + b"\n"
            for row in range(205, 210)
        ))

    second_plan = _rotation_plan(144)
    second = evolution_infra.archive_rotate_files(144, second_plan)

    assert second[0]["start_offset"] == first[0]["end_offset"]
    assert evolution_infra.validate_archive_rotation_receipts(
        143, first, rotation_plan=first_plan
    ) == first
    assert evolution_infra.validate_archive_rotation_receipts(
        144, second, rotation_plan=second_plan
    ) == second


def test_legacy_whole_generation_log_cleanup_is_retired(rotation_runtime):
    results, _archive, _source = rotation_runtime
    sibling = results / "v143" / "result.json"
    sibling.parent.mkdir()
    sibling.write_text("keep", encoding="utf-8")

    with pytest.raises(RuntimeError, match="retired"):
        evolution_infra.archive_old_logs()

    assert sibling.read_text(encoding="utf-8") == "keep"
