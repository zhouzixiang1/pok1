from pathlib import Path

from conftest import run_git
from worker_mcp.schemas import ExecutionProfile, TaskEnvelope, TaskType
from worker_mcp.worktree import WorktreeManager


def test_untracked_file_is_in_diff_and_forbidden_change_is_rejected(worker_config, git_repo):
    manager = WorktreeManager(worker_config)
    request = TaskEnvelope(
        goal="write file",
        context="",
        repo=str(git_repo),
        base_commit=run_git(git_repo, "rev-parse", "HEAD"),
        allowed_paths=["src"],
        forbidden_paths=["archive"],
        constraints=[],
        acceptance_criteria=[],
        execution=ExecutionProfile(read_only=False),
        idempotency_key="untracked-diff-0001",
        task_type=TaskType.PATCH,
    )
    request = manager.validate_request(request)
    path = manager.prepare(request, "untracked-task")
    (path / "src" / "new.py").write_text("NEW = True\n", encoding="utf-8")
    snapshot = manager.snapshot(path)
    assert "src/new.py" in snapshot.changed_files
    assert "NEW = True" in snapshot.diff
    (path / "archive" / "retired.py").write_text("CHANGED = True\n", encoding="utf-8")
    violations = manager.verify_changed_scope(request, manager.snapshot(path))
    assert any("archive/retired.py" in violation for violation in violations)
