"""Archived Worker-boundary tests for the retired multi-file candidate ABI."""

import os

import pytest

import worker_boundary
from worker_boundary import (
    audit_changed_files_against_plan,
    audit_worker_boundary,
    diff_snapshot,
    hash_changed_files,
    restore_python_files,
    snapshot_python_files,
)


def test_worker_boundary_rejects_undeclared_file_change(tmp_path):
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "strategy.py").write_text("A = 1\n", encoding="utf-8")
    (bot / "postflop.py").write_text("B = 1\n", encoding="utf-8")

    before = snapshot_python_files(bot)
    (bot / "strategy.py").write_text("A = 2\n", encoding="utf-8")
    (bot / "postflop.py").write_text("B = 2\n", encoding="utf-8")

    result = audit_worker_boundary(
        bot,
        {"target_files": ["strategy.py"], "files_allowed": []},
        before,
        next_v=250,
    )

    assert not result.passed
    assert "postflop.py" in result.changed_files
    assert result.violations == ["postflop.py: changed outside declared target_files/files_allowed"]

    restore_python_files(bot, before, result.changed_files)
    assert (bot / "strategy.py").read_text(encoding="utf-8") == "A = 1\n"
    assert (bot / "postflop.py").read_text(encoding="utf-8") == "B = 1\n"


def test_worker_boundary_ignores_parallel_sibling_changes(tmp_path):
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "constants.py").write_text("A = 1\n", encoding="utf-8")
    (bot / "opponent.py").write_text("B = 1\n", encoding="utf-8")
    (bot / "strategy.py").write_text("C = 1\n", encoding="utf-8")

    before = snapshot_python_files(bot)
    (bot / "constants.py").write_text("A = 2\n", encoding="utf-8")
    (bot / "opponent.py").write_text("B = 2\n", encoding="utf-8")
    (bot / "strategy.py").write_text("C = 2\n", encoding="utf-8")

    result = audit_worker_boundary(
        bot,
        {
            "role": "Hyperparameter Tuner",
            "target_files": ["constants.py"],
            "files_allowed": ["opponent.py", "strategy.py"],
        },
        before,
        next_v=250,
        ignored_changed_files=["opponent.py", "strategy.py"],
    )

    assert result.passed
    assert result.allowed_files == ["constants.py"]
    assert result.changed_files == ["constants.py", "opponent.py", "strategy.py"]
    assert result.ignored_changed_files == ["opponent.py", "strategy.py"]
    assert result.violation_files == []
    assert result.violations == []


def test_candidate_scope_audit_uses_master_plan_targets():
    result = audit_changed_files_against_plan(
        ["strategy.py", "postflop.py"],
        [{"role": "Algorithmic Logic Architect", "target_files": ["strategy.py"], "files_allowed": ["postflop.py"]}],
        next_v=250,
    )

    assert result.passed
    assert result.allowed_files == ["postflop.py", "strategy.py"]


def test_candidate_scope_audit_rejects_unplanned_file():
    result = audit_changed_files_against_plan(
        ["strategy.py", "opponent.py"],
        [{"target_files": ["strategy.py"], "files_allowed": []}],
        next_v=250,
    )

    assert not result.passed
    assert result.violations == ["opponent.py: changed outside master plan target_files/files_allowed"]


def test_tuner_files_allowed_cannot_expand_scope():
    result = audit_changed_files_against_plan(
        ["constants.py", "strategy.py"],
        [{
            "role": "Hyperparameter Tuner",
            "target_files": ["constants.py"],
            "files_allowed": ["strategy.py"],
        }],
        next_v=250,
    )

    assert not result.passed
    assert result.allowed_files == ["constants.py"]
    assert result.violations == ["strategy.py: changed outside master plan target_files/files_allowed"]


def test_worker_boundary_accepts_declared_nested_binary_invalid_utf8(tmp_path):
    bot = tmp_path / "bot"
    table = bot / "tables" / "equity.bin"
    table.parent.mkdir(parents=True)
    original = b"\x00\xff\x80equity-v1\x00"
    updated = b"\x00\xfe\x81equity-v2\x00"
    table.write_bytes(original)

    before = snapshot_python_files(bot)
    table.write_bytes(updated)
    result = audit_worker_boundary(
        bot,
        {"target_files": ["tables/equity.bin"]},
        before,
        next_v=250,
    )

    assert result.passed
    assert result.changed_files == ["tables/equity.bin"]
    assert before["tables/equity.bin"] == original

    restore_python_files(bot, before, result.changed_files)
    assert table.read_bytes() == original


def test_worker_boundary_allows_new_ancestors_for_declared_nested_binary(tmp_path):
    bot = tmp_path / "bot"
    bot.mkdir()
    before = snapshot_python_files(bot)
    table = bot / "tables" / "nested" / "equity.bin"
    table.parent.mkdir(parents=True)
    table.write_bytes(b"\xffnew-table")

    result = audit_worker_boundary(
        bot,
        {"target_files": ["tables/nested/equity.bin"]},
        before,
        next_v=250,
    )

    assert result.passed
    assert result.changed_files == [
        "tables",
        "tables/nested",
        "tables/nested/equity.bin",
    ]


def test_worker_boundary_does_not_authorize_empty_target_ancestor(tmp_path):
    bot = tmp_path / "bot"
    bot.mkdir()
    before = snapshot_python_files(bot)
    (bot / "tables").mkdir()

    result = audit_worker_boundary(
        bot,
        {"target_files": ["tables/equity.bin"]},
        before,
        next_v=250,
    )

    assert not result.passed
    assert result.violation_files == ["tables"]


def test_worker_boundary_rejects_and_restores_undeclared_nested_binary(tmp_path):
    bot = tmp_path / "bot"
    tables = bot / "tables"
    tables.mkdir(parents=True)
    (bot / "strategy.py").write_text("A = 1\n", encoding="utf-8")
    (tables / "equity.bin").write_bytes(b"trusted\x00table")

    before = snapshot_python_files(bot)
    rogue = tables / "rogue.bin"
    rogue.write_bytes(b"\xff\x00undeclared")
    result = audit_worker_boundary(
        bot,
        {"target_files": ["strategy.py"]},
        before,
        next_v=250,
    )

    assert not result.passed
    assert result.violation_files == ["tables/rogue.bin"]
    assert result.violations == [
        "tables/rogue.bin: changed outside declared target_files/files_allowed"
    ]

    restore_python_files(bot, before, result.violation_files)
    assert not rogue.exists()
    assert diff_snapshot(bot, before) == []


def test_artifact_snapshot_uses_publication_exclusions(tmp_path):
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "strategy.py").write_bytes(b"strategy-v1")
    (bot / ".completed").write_bytes(b"runtime-v1")
    for excluded in ("__pycache__", ".pytest_cache", ".task_context"):
        directory = bot / excluded
        directory.mkdir()
        (directory / "cache.bin").write_bytes(b"cache-v1")

    before = snapshot_python_files(bot)
    (bot / ".completed").write_bytes(b"runtime-v2")
    for excluded in ("__pycache__", ".pytest_cache", ".task_context"):
        (bot / excluded / "cache.bin").write_bytes(b"cache-v2")

    assert list(before) == ["strategy.py"]
    assert diff_snapshot(bot, before) == []


def test_worker_boundary_rejects_symlink_and_restores_binary_target(tmp_path):
    bot = tmp_path / "bot"
    table = bot / "tables" / "equity.bin"
    table.parent.mkdir(parents=True)
    original = b"\xfftrusted"
    table.write_bytes(original)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")

    before = snapshot_python_files(bot)
    table.unlink()
    table.symlink_to(outside)
    result = audit_worker_boundary(
        bot,
        {"target_files": ["tables/equity.bin"]},
        before,
        next_v=250,
    )

    assert not result.passed
    assert result.artifact_integrity_failed
    assert result.violation_files == ["tables/equity.bin"]
    assert "symbolic links are forbidden" in result.violations[0]

    restore_python_files(bot, before, result.violation_files)
    assert not table.is_symlink()
    assert table.read_bytes() == original
    assert outside.read_bytes() == b"outside"


def test_worker_boundary_rejects_special_file_even_in_excluded_tree(tmp_path):
    bot = tmp_path / "bot"
    cache = bot / "__pycache__"
    cache.mkdir(parents=True)
    (bot / "strategy.py").write_text("A = 1\n", encoding="utf-8")
    before = snapshot_python_files(bot)
    fifo = cache / "worker.fifo"
    os.mkfifo(fifo)

    result = audit_worker_boundary(
        bot,
        {"target_files": ["strategy.py"]},
        before,
        next_v=250,
    )

    assert not result.passed
    assert result.artifact_integrity_failed
    assert result.violation_files == ["__pycache__/worker.fifo"]

    restore_python_files(bot, before, result.violation_files)
    assert not fifo.exists()


def test_changed_file_hash_uses_streamed_manifest_not_full_byte_snapshot(
    tmp_path, monkeypatch
):
    bot = tmp_path / "bot"
    bot.mkdir()

    monkeypatch.setattr(
        worker_boundary,
        "snapshot_artifact_files",
        lambda _root: (_ for _ in ()).throw(AssertionError("full byte snapshot used")),
    )
    monkeypatch.setattr(
        worker_boundary,
        "artifact_manifest",
        lambda _root: {
            "entries": [
                {"path": ".", "type": "directory"},
                {
                    "path": "tables/equity.bin",
                    "type": "file",
                    "size": 2_000_000_000,
                    "sha256": "a" * 64,
                },
            ]
        },
    )

    digest = hash_changed_files(bot, ["tables/equity.bin", "deleted.bin"])

    assert len(digest) == 64


def test_snapshot_rejects_sparse_huge_file_before_read_or_allocation(
    tmp_path, monkeypatch
):
    bot = tmp_path / "bot"
    bot.mkdir()
    huge = bot / "tables.bin"
    with huge.open("wb") as handle:
        handle.truncate(100 * 1024 * 1024 * 1024)

    with pytest.raises(OSError, match="per-file byte limit exceeded"):
        worker_boundary.read_regular_file_bytes(bot, huge, huge.lstat())

    monkeypatch.setattr(
        worker_boundary,
        "read_regular_file_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("oversized file reached payload read")
        ),
    )

    with pytest.raises(worker_boundary.ArtifactSnapshotError) as caught:
        snapshot_python_files(bot)

    assert caught.value.violation_files == ["tables.bin"]
    assert "per-file byte limit exceeded" in str(caught.value)


def test_snapshot_rejects_total_bytes_before_reading_any_payload(
    tmp_path, monkeypatch
):
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "a.bin").write_bytes(b"a" * 8)
    (bot / "b.bin").write_bytes(b"b" * 8)
    monkeypatch.setattr(worker_boundary, "WORKER_SNAPSHOT_MAX_FILE_BYTES", 8)
    monkeypatch.setattr(worker_boundary, "WORKER_SNAPSHOT_MAX_TOTAL_BYTES", 15)
    monkeypatch.setattr(
        worker_boundary,
        "read_regular_file_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("total overflow reached payload read")
        ),
    )

    with pytest.raises(worker_boundary.ArtifactSnapshotError) as caught:
        snapshot_python_files(bot)

    assert "snapshot total byte limit exceeded" in str(caught.value)


def test_snapshot_rejects_file_count_before_reading_any_payload(
    tmp_path, monkeypatch
):
    bot = tmp_path / "bot"
    bot.mkdir()
    for index in range(3):
        (bot / f"table-{index}.bin").write_bytes(bytes([index]))
    monkeypatch.setattr(worker_boundary, "WORKER_SNAPSHOT_MAX_FILE_COUNT", 2)
    monkeypatch.setattr(worker_boundary, "WORKER_SNAPSHOT_MAX_ENTRY_COUNT", 10)
    monkeypatch.setattr(
        worker_boundary,
        "read_regular_file_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("file-count overflow reached payload read")
        ),
    )

    with pytest.raises(worker_boundary.ArtifactSnapshotError) as caught:
        snapshot_python_files(bot)

    assert "snapshot file-count limit exceeded" in str(caught.value)


def test_snapshot_accepts_exact_count_file_and_total_byte_limits(
    tmp_path, monkeypatch
):
    bot = tmp_path / "bot"
    bot.mkdir()
    first = b"\x00\xff123456"
    second = b"strategy"
    (bot / "equity.bin").write_bytes(first)
    (bot / "strategy.py").write_bytes(second)
    monkeypatch.setattr(worker_boundary, "WORKER_SNAPSHOT_MAX_FILE_COUNT", 2)
    monkeypatch.setattr(worker_boundary, "WORKER_SNAPSHOT_MAX_ENTRY_COUNT", 2)
    monkeypatch.setattr(worker_boundary, "WORKER_SNAPSHOT_MAX_FILE_BYTES", 8)
    monkeypatch.setattr(worker_boundary, "WORKER_SNAPSHOT_MAX_TOTAL_BYTES", 16)

    snapshot = snapshot_python_files(bot)

    assert snapshot["equity.bin"] == first
    assert snapshot["strategy.py"] == second
