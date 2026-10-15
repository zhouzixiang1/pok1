"""Batch-cap and bounded-memory guards for JSONL cold-prefix rotation (2026-10-15).

When archive rotation resumes after the long v27+ handoff-gate stall, the
events.jsonl cold prefix is ~933 MB against a ~16 MB watermark.  These tests
pin the disk/RSS spike guards added to
``evolution_infra_archive_rotation``:

1. one rotation batch is clamped to ``_ROTATION_BATCH_MAX_BYTES`` (env
   overridable, hardcoded default) in BOTH ``build_archive_rotation_plan``
   and the mirror recompute inside ``validate_archive_rotation_plan`` --
   otherwise the mirror would report ``frozen range changed``;
2. consecutive publications drain an over-cap cold prefix in cap-sized
   batches with exact byte conservation (batches concatenated equal the
   full cold prefix) while the live source stays append-only;
3. a cap larger than the cold prefix is byte-for-byte identical to the
   historical uncapped behaviour;
4. the bounded backward line-boundary scan and the streamed view digests
   are equivalent to the historical whole-buffer ``splitlines`` /
   ``sha256`` semantics.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import random

import pytest

import evolution_infra
import evolution_infra_archive_rotation as rotation_module

_PUBLICATION_ID = "c" * 64
_CAP_ENV = rotation_module._ROTATION_BATCH_MAX_BYTES_ENV
_DEFAULT_SCAN_CHUNK = rotation_module._ROTATION_SCAN_CHUNK_BYTES


def _legacy_cold_prefix_end(data: bytes, keep_lines: int, limit=None) -> int:
    """The historical whole-buffer expression, kept verbatim as the oracle."""

    if limit is not None:
        data = data[:limit]
    lines = data.splitlines(keepends=True)
    return (
        sum(len(line) for line in lines[:-keep_lines])
        if len(lines) > keep_lines
        else 0
    )


class _BytesRangeReader:
    """In-memory stand-in with the view's ``read_range`` shape."""

    def __init__(self, data: bytes):
        self._data = data

    def read_range(self, start: int, end: int) -> bytes:
        return self._data[start:end]


# ──────────────────────────────────────────────
# Equivalence: bounded scan / streamed view vs historical bytes semantics.
# ──────────────────────────────────────────────


def test_backward_scan_equals_splitlines_math_exhaustively():
    alphabet = (b"a", b"\n", b"\r")
    checked = 0
    for size in range(0, 9):
        for combo in itertools.product(alphabet, repeat=size):
            data = b"".join(combo)
            for keep_lines in (1, 2, 3):
                expected = _legacy_cold_prefix_end(data, keep_lines)
                observed = rotation_module._scan_cold_end_backwards(
                    _BytesRangeReader(data).read_range,
                    len(data),
                    keep_lines,
                )
                assert observed == expected, (data, keep_lines)
                checked += 1
    assert checked == 29523


def test_backward_scan_survives_chunk_boundaries_random(monkeypatch):
    rng = random.Random(20261015)
    for _ in range(2000):
        size = rng.randrange(0, 48)
        data = bytes(
            rng.choice((0x61, 0x62, 0x0A, 0x0D, 0x0D, 0x0A))
            for _ in range(size)
        )
        for keep_lines in (1, 2, 4):
            expected = _legacy_cold_prefix_end(data, keep_lines)
            for chunk in (1, 2, 3, 7):
                monkeypatch.setattr(
                    rotation_module,
                    "_ROTATION_SCAN_CHUNK_BYTES",
                    chunk,
                )
                observed = rotation_module._scan_cold_end_backwards(
                    _BytesRangeReader(data).read_range,
                    len(data),
                    keep_lines,
                )
                assert observed == expected, (data, keep_lines, chunk)


def test_source_view_matches_bytes_slicing_and_digests(tmp_path, monkeypatch):
    rng = random.Random(7)
    data = b"".join(
        bytes([rng.choice((0x61, 0x62, 0x0A, 0x0D))]) for _ in range(4096)
    ) + b"\ntail-unterminated"
    path = tmp_path / "source.jsonl"
    path.write_bytes(data)

    view = rotation_module._RotationSourceView.open(path)
    try:
        assert len(view) == len(data)
        assert view[0:0] == b""
        assert view[5:3] == b""
        assert view[7:99] == data[7:99]
        assert view[: len(data) + 100] == data
        assert view[123:] == data[123:]
        assert (
            view.digest_range(0, len(data)) == hashlib.sha256(data).hexdigest()
        )
        assert view.digest_range(100, 100) == hashlib.sha256(b"").hexdigest()
        assert view.digest_range(17, 900) == hashlib.sha256(
            data[17:900]
        ).hexdigest()
        for keep_lines in (1, 3):
            assert rotation_module._rotation_cold_prefix_end(
                view, keep_lines
            ) == _legacy_cold_prefix_end(data, keep_lines)
        assert rotation_module._rotation_digest_range(
            view, 3, 2000
        ) == evolution_infra._rotation_digest(data[3:2000])
    finally:
        view.close()

    # The bytes-input path of the shared helpers is unchanged.
    assert rotation_module._rotation_digest_range(
        data, 10, 20
    ) == evolution_infra._rotation_digest(data[10:20])
    assert rotation_module._rotation_cold_prefix_end(
        data, 2, limit=100
    ) == _legacy_cold_prefix_end(data, 2, limit=100)


def test_source_view_cold_scan_survives_stream_chunk_edges(
    tmp_path, monkeypatch
):
    rng = random.Random(11)
    for _ in range(150):
        size = rng.randrange(0, 64)
        data = bytes(
            rng.choice((0x61, 0x0A, 0x0D, 0x0D)) for _ in range(size)
        )
        path = tmp_path / "source.jsonl"
        path.write_bytes(data)
        view = rotation_module._RotationSourceView.open(path)
        try:
            for keep_lines in (1, 2):
                assert rotation_module._rotation_cold_prefix_end(
                    view, keep_lines
                ) == _legacy_cold_prefix_end(data, keep_lines)
        finally:
            view.close()


def test_source_view_rejects_stepped_slices(tmp_path):
    path = tmp_path / "source.jsonl"
    path.write_bytes(b"abcdef\n")
    view = rotation_module._RotationSourceView.open(path)
    try:
        with pytest.raises(TypeError):
            view[::2]
        with pytest.raises(TypeError):
            view[0]
    finally:
        view.close()


# ──────────────────────────────────────────────
# Cap resolution and clamp arithmetic.
# ──────────────────────────────────────────────


def test_batch_cap_resolution_env_overrides_and_fallbacks(monkeypatch):
    default = rotation_module._ROTATION_BATCH_MAX_BYTES
    assert default == 128 * 1024 * 1024
    monkeypatch.delenv(_CAP_ENV, raising=False)
    assert rotation_module._rotation_batch_cap_bytes() == default

    monkeypatch.setenv(_CAP_ENV, "4096")
    assert rotation_module._rotation_batch_cap_bytes() == 4096

    monkeypatch.setenv(_CAP_ENV, "not-a-number")
    assert rotation_module._rotation_batch_cap_bytes() == default

    monkeypatch.setenv(_CAP_ENV, "0")
    assert rotation_module._rotation_batch_cap_bytes() == default

    monkeypatch.setenv(_CAP_ENV, "-1024")
    assert rotation_module._rotation_batch_cap_bytes() == default


def test_clamp_cold_end_arithmetic(monkeypatch):
    monkeypatch.setenv(_CAP_ENV, "1000")
    assert rotation_module._rotation_clamp_cold_end(999, 0) == 999
    assert rotation_module._rotation_clamp_cold_end(1000, 0) == 1000
    assert rotation_module._rotation_clamp_cold_end(2500, 0) == 1000
    # The clamp is relative to the watermark start, not absolute.
    assert rotation_module._rotation_clamp_cold_end(
        16_000_000 + 2500, 16_000_000
    ) == 16_000_000 + 1000
    # No-op for an empty rotation range.
    assert rotation_module._rotation_clamp_cold_end(500, 500) == 500


# ──────────────────────────────────────────────
# Pipeline behaviour under the cap.
# ──────────────────────────────────────────────


@pytest.fixture
def rotation_runtime(tmp_path, monkeypatch):
    results = tmp_path / "results"
    archive = results / "archive"
    results.mkdir()
    for name, filename in (
        ("WORKER_FAILURES_FILE", "worker_failures.jsonl"),
        ("MATCH_HISTORY_FILE", "match_history.jsonl"),
        ("RATING_HISTORY_FILE", "rating_history.jsonl"),
        ("LLM_COSTS_FILE", "llm_costs.jsonl"),
    ):
        monkeypatch.setattr(
            evolution_infra,
            name,
            results / filename,
        )
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "ARCHIVE_DIR", archive)
    return results, archive


def _seed_events_source(
    results,
    *,
    cold_bytes: int,
    keep_lines: int,
    line_len: int,
):
    """Write events.jsonl whose cold prefix is exactly ``cold_bytes``."""

    assert line_len >= 14
    assert cold_bytes % line_len == 0
    cold_lines = cold_bytes // line_len
    total_lines = cold_lines + keep_lines
    pad = b"x" * (line_len - 14)
    line = b'{"row":%5d}' + pad + b"\n"
    data = b"".join(line % (row % 100000) for row in range(total_lines))
    assert len(data) == total_lines * line_len
    source = results / "events.jsonl"
    source.write_bytes(data)
    return source, data


def _snapshot_for(plan: dict, source_name: str) -> dict:
    return next(
        item
        for item in plan["source_snapshots"]
        if item["source"] == source_name
    )


def test_build_clamps_large_cold_prefix_and_validate_mirror_agrees(
    rotation_runtime,
    monkeypatch,
):
    results, archive = rotation_runtime
    line_len, keep_lines, cap = 64, 1000, 8193
    cold_bytes = 40 * 1024  # ~5x cap: a synthetic large cold prefix.
    source, data = _seed_events_source(
        results,
        cold_bytes=cold_bytes,
        keep_lines=keep_lines,
        line_len=line_len,
    )
    monkeypatch.setenv(_CAP_ENV, str(cap))

    plan = evolution_infra.build_archive_rotation_plan(143, _PUBLICATION_ID)
    snapshot = _snapshot_for(plan, "events.jsonl")
    expected = snapshot["expected_rotation"]
    assert expected is not None
    # The planned batch is exactly one cap from the (zero) watermark start
    # (cutting mid-line is fine: copy-only byte semantics), where the
    # uncapped cold prefix would have been 40 KiB.
    assert expected["start_offset"] == 0
    assert expected["end_offset"] == cap
    assert snapshot["cold_end_offset"] == cap
    assert snapshot["snapshot_size"] == len(data)
    assert expected["archive_sha256"] == hashlib.sha256(
        data[:cap]
    ).hexdigest()
    assert expected["new_prefix_sha256"] == hashlib.sha256(
        data[:cap]
    ).hexdigest()
    assert snapshot["snapshot_sha256"] == hashlib.sha256(data).hexdigest()
    assert (archive / "rotation-set-v143.plan.json").is_file()

    # The validate mirror recomputes the SAME capped range: it must not
    # report "frozen range changed".
    assert evolution_infra.validate_archive_rotation_plan(
        plan,
        version=143,
        publication_id=_PUBLICATION_ID,
    ) == plan

    receipts = evolution_infra.archive_rotate_files(143, plan)
    assert receipts == evolution_infra.expected_archive_rotation_receipts(
        plan, version=143, publication_id=_PUBLICATION_ID
    )
    assert (archive / "events_v143.jsonl").read_bytes() == data[:cap]
    assert source.read_bytes() == data
    assert evolution_infra.validate_archive_rotation_receipts(
        143, receipts, rotation_plan=plan
    ) == receipts


def test_two_publications_drain_oversized_prefix_with_byte_conservation(
    rotation_runtime,
    monkeypatch,
):
    results, archive = rotation_runtime
    line_len, keep_lines, cap = 50, 1000, 8027
    cold_bytes = 12800  # ~1.6x cap: two publications must drain it.
    source, data = _seed_events_source(
        results,
        cold_bytes=cold_bytes,
        keep_lines=keep_lines,
        line_len=line_len,
    )
    monkeypatch.setenv(_CAP_ENV, str(cap))

    first_plan = evolution_infra.build_archive_rotation_plan(
        143, _PUBLICATION_ID
    )
    first = evolution_infra.archive_rotate_files(143, first_plan)
    assert first[0]["start_offset"] == 0
    assert first[0]["end_offset"] == cap

    second_plan = evolution_infra.build_archive_rotation_plan(144, "d" * 64)
    second = evolution_infra.archive_rotate_files(144, second_plan)
    # Watermark advanced: the second batch continues exactly where the
    # first stopped and drains the remainder in one pass.
    assert second[0]["start_offset"] == first[0]["end_offset"]
    assert second[0]["end_offset"] == cold_bytes
    assert cold_bytes - cap < cap

    # Byte conservation: the two archive slices concatenate to the full
    # cold prefix, and the live source is untouched.
    batch_one = (archive / "events_v143.jsonl").read_bytes()
    batch_two = (archive / "events_v144.jsonl").read_bytes()
    assert batch_one == data[:cap]
    assert batch_two == data[cap:cold_bytes]
    assert batch_one + batch_two == data[:cold_bytes]
    assert source.read_bytes() == data

    watermark = json.loads(
        (archive / "events.rotation-watermark.json").read_text(
            encoding="utf-8"
        )
    )
    assert watermark["end_offset"] == cold_bytes
    assert evolution_infra.validate_archive_rotation_receipts(
        143, first, rotation_plan=first_plan
    ) == first
    assert evolution_infra.validate_archive_rotation_receipts(
        144, second, rotation_plan=second_plan
    ) == second

    # A third publication with no further appends finds nothing left to do.
    third_plan = evolution_infra.build_archive_rotation_plan(145, "e" * 64)
    assert (
        _snapshot_for(third_plan, "events.jsonl")["expected_rotation"] is None
    )
    assert third_plan["expected_rotations"] == []
    assert evolution_infra.validate_archive_rotation_receipts(
        145, [], rotation_plan=third_plan
    ) == []


def test_three_publications_drain_two_and_half_cap_backlog(
    rotation_runtime,
    monkeypatch,
):
    results, archive = rotation_runtime
    line_len, keep_lines, cap = 46, 1000, 4600
    cold_bytes = 11500  # exactly 2.5x cap.
    source, data = _seed_events_source(
        results,
        cold_bytes=cold_bytes,
        keep_lines=keep_lines,
        line_len=line_len,
    )
    monkeypatch.setenv(_CAP_ENV, str(cap))

    batches = []
    ends = []
    for version, pub_id in ((143, "1" * 64), (144, "2" * 64), (145, "3" * 64)):
        plan = evolution_infra.build_archive_rotation_plan(version, pub_id)
        receipts = evolution_infra.archive_rotate_files(version, plan)
        assert len(receipts) == 1
        batches.append((archive / f"events_v{version}.jsonl").read_bytes())
        ends.append(receipts[0]["end_offset"])
        assert evolution_infra.validate_archive_rotation_receipts(
            version, receipts, rotation_plan=plan
        ) == receipts

    assert ends == [cap, 2 * cap, cold_bytes]
    assert b"".join(batches) == data[:cold_bytes]
    assert source.read_bytes() == data


def test_cap_larger_than_cold_prefix_is_byte_identical_to_uncapped(
    rotation_runtime,
    monkeypatch,
):
    results, archive = rotation_runtime
    # The classic fixture shape: 205 worker-failure lines against keep=200,
    # a 5-line cold prefix, with the default 128 MiB cap in force.
    monkeypatch.delenv(_CAP_ENV, raising=False)
    source = results / "worker_failures.jsonl"
    data = b"".join(
        json.dumps({"row": row}).encode("utf-8") + b"\n"
        for row in range(205)
    )
    source.write_bytes(data)
    legacy_cold_end = _legacy_cold_prefix_end(data, 200)
    assert legacy_cold_end > 0

    plan = evolution_infra.build_archive_rotation_plan(143, _PUBLICATION_ID)
    snapshot = _snapshot_for(plan, "worker_failures.jsonl")
    expected = snapshot["expected_rotation"]
    assert expected is not None
    assert expected["start_offset"] == 0
    assert expected["end_offset"] == legacy_cold_end
    assert expected["archive_sha256"] == hashlib.sha256(
        data[:legacy_cold_end]
    ).hexdigest()
    assert expected["new_prefix_sha256"] == hashlib.sha256(
        data[:legacy_cold_end]
    ).hexdigest()
    assert snapshot["snapshot_size"] == len(data)
    assert snapshot["snapshot_sha256"] == hashlib.sha256(data).hexdigest()
    assert evolution_infra.validate_archive_rotation_plan(
        plan, version=143, publication_id=_PUBLICATION_ID
    ) == plan

    receipts = evolution_infra.archive_rotate_files(143, plan)
    assert (archive / "worker_failures_v143.jsonl").read_bytes() == (
        data[:legacy_cold_end]
    )
    assert source.read_bytes() == data
    assert evolution_infra.validate_archive_rotation_receipts(
        143, receipts, rotation_plan=plan
    ) == receipts


def test_rotation_never_resizes_live_sources_under_cap(
    rotation_runtime,
    monkeypatch,
):
    """copy-only semantics: source bytes and size are preserved per batch."""

    results, archive = rotation_runtime
    line_len, keep_lines, cap = 32, 1000, 4096
    cold_bytes = cap * 2
    source, data = _seed_events_source(
        results,
        cold_bytes=cold_bytes,
        keep_lines=keep_lines,
        line_len=line_len,
    )
    monkeypatch.setenv(_CAP_ENV, str(cap))
    original_size = source.stat().st_size

    for version, pub_id in ((143, "a" * 64), (144, "b" * 64)):
        plan = evolution_infra.build_archive_rotation_plan(version, pub_id)
        evolution_infra.archive_rotate_files(version, plan)
        assert source.read_bytes() == data
        assert source.stat().st_size == original_size


def test_source_view_cold_scan_honours_frozen_size_limit(tmp_path):
    # validate_archive_rotation_plan scans the view with
    # limit=snapshot_size whenever the live source has appended past the
    # frozen plan size; the other view tests only exercised limit=None (the
    # audit of 2026-10-15 flagged this gap).
    rng = random.Random(23)
    frozen = bytes(rng.choice((0x61, 0x0A, 0x0D)) for _ in range(300)) + b"\n"
    appended = bytes(rng.choice((0x62, 0x0A)) for _ in range(200)) + b"\n"
    path = tmp_path / "source.jsonl"
    path.write_bytes(frozen + appended)

    view = rotation_module._RotationSourceView.open(path)
    try:
        for keep_lines in (1, 2, 5):
            assert rotation_module._rotation_cold_prefix_end(
                view, keep_lines, limit=len(frozen)
            ) == _legacy_cold_prefix_end(frozen, keep_lines), keep_lines
        # limit=None still sees the whole live file.
        assert rotation_module._rotation_cold_prefix_end(
            view, 1
        ) == _legacy_cold_prefix_end(frozen + appended, 1)
    finally:
        view.close()
