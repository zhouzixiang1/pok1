from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOOL = (
    ROOT
    / "bots"
    / "neural_national_lab"
    / "tools"
    / "longrun_collect_oppmodel.py"
)


def _load_tool():
    spec = importlib.util.spec_from_file_location("longrun_collect_oppmodel", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deck_seed_blocks_do_not_overlap_across_tasks_or_passes() -> None:
    tool = _load_tool()
    hands = 70
    guard = 10
    blocks = []
    for pass_index in range(3):
        for task_index in range(6):
            start = tool._deck_seed_for_task(
                root=5_000_000,
                pass_index=pass_index,
                task_index=task_index,
                hands=hands,
                guard=guard,
            )
            blocks.append(set(range(start, start + hands)))

    assert all(
        left.isdisjoint(right)
        for left_index, left in enumerate(blocks)
        for right in blocks[left_index + 1:]
    )


def test_deck_and_bot_seed_plans_are_resume_deterministic() -> None:
    tool = _load_tool()
    kwargs = {
        "root": 5_000_000,
        "pass_index": 17,
        "task_index": 5,
        "hands": 70,
        "guard": 10,
    }

    assert tool._deck_seed_for_task(**kwargs) == tool._deck_seed_for_task(**kwargs)
    assert tool._bot_seed_for_task(
        root=1_000_000, pass_index=17, task_index=5
    ) == tool._bot_seed_for_task(
        root=1_000_000, pass_index=17, task_index=5
    )


def test_seed_plan_rejects_task_outside_reserved_pass_slots() -> None:
    tool = _load_tool()

    with pytest.raises(ValueError, match="reserved deck-seed slots"):
        tool._deck_seed_for_task(
            root=5_000_000,
            pass_index=0,
            task_index=tool.DECK_SEED_SLOTS_PER_PASS,
            hands=70,
            guard=10,
        )


def test_execution_snapshot_excludes_runtime_markers_and_is_read_only(
    tmp_path: Path,
) -> None:
    tool = _load_tool()
    source = tmp_path / "source"
    source.mkdir()
    (source / "national_bot.py").write_text("print('ok')\n", encoding="utf-8")
    (source / ".completed").write_text("runtime\n", encoding="utf-8")
    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "national_bot.pyc").write_bytes(b"runtime")
    destination = tmp_path / "snapshot" / "national_v1"

    expected = tool._directory_digest(source)
    tool._copy_opponent_snapshot(source, destination)

    assert tool._directory_digest(destination) == expected
    assert not (destination / ".completed").exists()
    assert not (destination / "__pycache__").exists()
    assert destination.stat().st_mode & 0o222 == 0
    assert (destination / "national_bot.py").stat().st_mode & 0o222 == 0
