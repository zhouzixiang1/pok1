from worker_boundary import (
    audit_changed_files_against_plan,
    audit_worker_boundary,
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


def test_candidate_scope_audit_uses_master_plan_targets():
    result = audit_changed_files_against_plan(
        ["strategy.py", "postflop.py"],
        [{"target_files": ["strategy.py"], "files_allowed": ["postflop.py"]}],
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
