"""Worker write-boundary tests for the strict national TCP policy ABI."""

import os

from bot_namespace import STRICT_ARTIFACT_FILES, strict_artifact_layout_errors
from worker_boundary import (
    audit_changed_files_against_plan,
    audit_worker_boundary,
    diff_snapshot,
    restore_python_files,
    snapshot_python_files,
)


_SYSTEM_FILES = frozenset(STRICT_ARTIFACT_FILES - {"policy.py"})


def _write_strict_bot(root):
    root.mkdir()
    payloads = {
        "national_bot.py": b"# system runtime\n",
        "precompute.py": b"# system precompute\n",
        "policy.py": b"def decide(context):\n    return {'kind': 'pass'}\n",
        "national_runtime_manifest.json": b"{}\n",
        "policy_epoch_receipt.json": b"{}\n",
    }
    assert frozenset(payloads) == STRICT_ARTIFACT_FILES
    for relative, payload in payloads.items():
        (root / relative).write_bytes(payload)
    assert strict_artifact_layout_errors(root) == []
    return payloads


def test_worker_boundary_accepts_only_policy_change_in_exact_five_file_bot(tmp_path):
    bot = tmp_path / "national_v143"
    original = _write_strict_bot(bot)
    before = snapshot_python_files(bot)

    (bot / "policy.py").write_text(
        "def decide(context):\n    return {'kind': 'allin'}\n",
        encoding="utf-8",
    )
    result = audit_worker_boundary(
        bot,
        {"target_files": ["policy.py"], "files_allowed": []},
        before,
        next_v=143,
    )

    assert result.passed
    assert result.allowed_files == ["policy.py"]
    assert result.changed_files == ["policy.py"]
    for relative in _SYSTEM_FILES:
        assert (bot / relative).read_bytes() == original[relative]


def test_stale_task_cannot_authorize_system_runtime_change(tmp_path):
    bot = tmp_path / "national_v143"
    original = _write_strict_bot(bot)
    before = snapshot_python_files(bot)

    (bot / "policy.py").write_bytes(b"# candidate edit\n")
    (bot / "national_bot.py").write_bytes(b"# stale worker rewrote runtime\n")
    result = audit_worker_boundary(
        bot,
        {
            "role": "Algorithmic Logic Architect",
            "target_files": ["policy.py"],
            "files_allowed": ["national_bot.py"],
        },
        before,
        next_v=143,
    )

    assert not result.passed
    assert result.allowed_files == ["policy.py"]
    assert result.violation_files == ["national_bot.py"]
    restore_python_files(bot, before, result.changed_files)
    for relative, payload in original.items():
        assert (bot / relative).read_bytes() == payload


def test_final_scope_audit_rejects_system_and_old_candidate_modules():
    result = audit_changed_files_against_plan(
        ["policy.py", "precompute.py", "strategy.py"],
        [{
            "target_files": ["policy.py"],
            "files_allowed": ["precompute.py", "strategy.py"],
        }],
        next_v=143,
    )

    assert not result.passed
    assert result.allowed_files == ["policy.py"]
    assert result.violation_files == ["precompute.py", "strategy.py"]


def test_artifact_snapshot_excludes_runtime_caches_but_tracks_five_files(tmp_path):
    bot = tmp_path / "national_v143"
    _write_strict_bot(bot)
    (bot / ".completed").write_bytes(b"runtime-v1")
    for excluded in ("__pycache__", ".pytest_cache", ".task_context"):
        directory = bot / excluded
        directory.mkdir()
        (directory / "cache.bin").write_bytes(b"cache-v1")

    before = snapshot_python_files(bot)
    (bot / ".completed").write_bytes(b"runtime-v2")
    for excluded in ("__pycache__", ".pytest_cache", ".task_context"):
        (bot / excluded / "cache.bin").write_bytes(b"cache-v2")

    assert set(before) == STRICT_ARTIFACT_FILES
    assert diff_snapshot(bot, before) == []


def test_policy_symlink_is_rejected_and_restored_without_following_it(tmp_path):
    bot = tmp_path / "national_v143"
    original = _write_strict_bot(bot)
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"outside\n")
    before = snapshot_python_files(bot)

    (bot / "policy.py").unlink()
    (bot / "policy.py").symlink_to(outside)
    result = audit_worker_boundary(
        bot,
        {"target_files": ["policy.py"]},
        before,
        next_v=143,
    )

    assert not result.passed
    assert result.artifact_integrity_failed
    assert result.violation_files == ["policy.py"]
    assert "symbolic links are forbidden" in result.violations[0]
    restore_python_files(bot, before, result.violation_files)
    assert not (bot / "policy.py").is_symlink()
    assert (bot / "policy.py").read_bytes() == original["policy.py"]
    assert outside.read_bytes() == b"outside\n"


def test_special_file_in_excluded_tree_is_still_rejected(tmp_path):
    bot = tmp_path / "national_v143"
    _write_strict_bot(bot)
    cache = bot / "__pycache__"
    cache.mkdir()
    before = snapshot_python_files(bot)
    fifo = cache / "worker.fifo"
    os.mkfifo(fifo)

    result = audit_worker_boundary(
        bot,
        {"target_files": ["policy.py"]},
        before,
        next_v=143,
    )

    assert not result.passed
    assert result.artifact_integrity_failed
    assert result.violation_files == ["__pycache__/worker.fifo"]
    restore_python_files(bot, before, result.violation_files)
    assert not fifo.exists()
