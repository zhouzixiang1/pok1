"""Tests for _incremental_reset_next_dir in core.tool_planning.

Verifies the incremental reset: overwrites files present in source (undoing worker
edits to existing files) while PRESERVING worker-created NEW files absent from source.
"""

from core.tool_planning import _incremental_reset_next_dir


def test_incremental_reset_overwrites_and_preserves(tmp_path):
    source_dir = tmp_path / "source"
    next_dir = tmp_path / "next"
    source_dir.mkdir()
    next_dir.mkdir()

    # Source: main.py + strategy.py
    (source_dir / "main.py").write_text("SOURCE_MAIN", encoding="utf-8")
    (source_dir / "strategy.py").write_text("SOURCE_STRATEGY", encoding="utf-8")

    # next: modified main.py, modified strategy.py, NEW new_module.py, .completed
    (next_dir / "main.py").write_text("MODIFIED_MAIN", encoding="utf-8")
    (next_dir / "strategy.py").write_text("MODIFIED_STRATEGY", encoding="utf-8")
    (next_dir / "new_module.py").write_text("NEW_MODULE_CONTENT", encoding="utf-8")
    (next_dir / ".completed").write_text("sentinel", encoding="utf-8")

    preserved = _incremental_reset_next_dir(next_dir, source_dir)

    # main.py and strategy.py overwritten to source versions
    assert (next_dir / "main.py").read_text(encoding="utf-8") == "SOURCE_MAIN"
    assert (next_dir / "strategy.py").read_text(encoding="utf-8") == "SOURCE_STRATEGY"

    # new_module.py preserved with original content
    assert (next_dir / "new_module.py").exists()
    assert (next_dir / "new_module.py").read_text(encoding="utf-8") == "NEW_MODULE_CONTENT"

    # .completed untouched
    assert (next_dir / ".completed").exists()
    assert (next_dir / ".completed").read_text(encoding="utf-8") == "sentinel"

    # returned list is exactly the NEW file
    assert preserved == ["new_module.py"]


def test_incremental_reset_removes_pycache(tmp_path):
    source_dir = tmp_path / "source"
    next_dir = tmp_path / "next"
    source_dir.mkdir()
    next_dir.mkdir()

    (source_dir / "main.py").write_text("SOURCE_MAIN", encoding="utf-8")
    (next_dir / "main.py").write_text("MODIFIED_MAIN", encoding="utf-8")

    # stale bytecode: __pycache__ dir + .pyc file
    pycache_dir = next_dir / "__pycache__"
    pycache_dir.mkdir()
    (pycache_dir / "main.cpython-311.pyc").write_text("bytecode", encoding="utf-8")
    (next_dir / "stale.pyc").write_text("bytecode", encoding="utf-8")

    # A NEW file should still be preserved even when __pycache__ is present
    (next_dir / "new_module.py").write_text("NEW_MODULE_CONTENT", encoding="utf-8")

    preserved = _incremental_reset_next_dir(next_dir, source_dir)

    # __pycache__ dir and .pyc file removed
    assert not pycache_dir.exists()
    assert not (next_dir / "stale.pyc").exists()

    # main.py overwritten to source version
    assert (next_dir / "main.py").read_text(encoding="utf-8") == "SOURCE_MAIN"

    # NEW file preserved
    assert (next_dir / "new_module.py").read_text(encoding="utf-8") == "NEW_MODULE_CONTENT"

    assert preserved == ["new_module.py"]


def test_incremental_reset_creates_source_only_files(tmp_path):
    source_dir = tmp_path / "source"
    next_dir = tmp_path / "next"
    source_dir.mkdir()
    next_dir.mkdir()

    # Source has a file absent from next
    (source_dir / "main.py").write_text("SOURCE_MAIN", encoding="utf-8")
    (source_dir / "helper.py").write_text("SOURCE_HELPER", encoding="utf-8")
    (next_dir / "main.py").write_text("MODIFIED_MAIN", encoding="utf-8")

    preserved = _incremental_reset_next_dir(next_dir, source_dir)

    # main.py overwritten, helper.py created
    assert (next_dir / "main.py").read_text(encoding="utf-8") == "SOURCE_MAIN"
    assert (next_dir / "helper.py").read_text(encoding="utf-8") == "SOURCE_HELPER"

    # No NEW files to preserve
    assert preserved == []
