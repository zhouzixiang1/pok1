from pathlib import Path

import pytest

from conftest import run_git
from worker_mcp.schemas import ExecutionProfile, TaskEnvelope, TaskType
from worker_mcp.worktree import WorktreeDirty, WorktreeError, WorktreeManager


def request(repo: Path, head: str, *, read_only=False):
    return TaskEnvelope(
        goal="patch source",
        context="",
        repo=str(repo),
        base_commit=head,
        allowed_paths=["src"],
        forbidden_paths=["archive"],
        constraints=[],
        acceptance_criteria=[],
        execution=ExecutionProfile(read_only=read_only),
        idempotency_key="worktree-test-0001",
        task_type=TaskType.ANALYZE if read_only else TaskType.PATCH,
    )


def test_owner_marked_worktree_diff_and_clean_cleanup(worker_config, git_repo):
    manager = WorktreeManager(worker_config)
    head = run_git(git_repo, "rev-parse", "HEAD")
    req = manager.validate_request(request(git_repo, head))
    path = manager.prepare(req, "task-one")
    assert manager.prepare(req, "task-one") == path
    (path / "src" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    snapshot = manager.snapshot(path)
    assert snapshot.dirty and snapshot.changed_files == ("src/module.py",)
    assert not manager.verify_changed_scope(req, snapshot)
    with pytest.raises(WorktreeDirty):
        manager.cleanup_owned_clean(
            task_id="task-one", repository=str(git_repo), worktree_path=str(path)
        )
    (path / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    manager.cleanup_owned_clean(
        task_id="task-one", repository=str(git_repo), worktree_path=str(path)
    )
    assert not path.exists()


def test_cleanup_rejects_wrong_owner(worker_config, git_repo):
    manager = WorktreeManager(worker_config)
    head = run_git(git_repo, "rev-parse", "HEAD")
    req = manager.validate_request(request(git_repo, head))
    path = manager.prepare(req, "task-two")
    with pytest.raises(WorktreeError):
        manager.cleanup_owned_clean(
            task_id="some-other-task", repository=str(git_repo), worktree_path=str(path)
        )
    manager.cleanup_owned_clean(
        task_id="task-two", repository=str(git_repo), worktree_path=str(path)
    )
