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
            task_id="task-one",
            repository=str(git_repo),
            base_commit=head,
            worktree_path=str(path),
        )
    (path / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    manager.cleanup_owned_clean(
        task_id="task-one",
        repository=str(git_repo),
        base_commit=head,
        worktree_path=str(path),
    )
    assert not path.exists()


def test_cleanup_rejects_wrong_owner(worker_config, git_repo):
    manager = WorktreeManager(worker_config)
    head = run_git(git_repo, "rev-parse", "HEAD")
    req = manager.validate_request(request(git_repo, head))
    path = manager.prepare(req, "task-two")
    with pytest.raises(WorktreeError):
        manager.cleanup_owned_clean(
            task_id="some-other-task",
            repository=str(git_repo),
            base_commit=head,
            worktree_path=str(path),
        )
    manager.cleanup_owned_clean(
        task_id="task-two",
        repository=str(git_repo),
        base_commit=head,
        worktree_path=str(path),
    )


def test_cleanup_rejects_head_that_diverged_from_durable_base(
    worker_config, git_repo
):
    manager = WorktreeManager(worker_config)
    base = run_git(git_repo, "rev-parse", "HEAD")
    req = manager.validate_request(request(git_repo, base))
    path = manager.prepare(req, "task-head-mismatch")
    (git_repo / "src" / "module.py").write_text("VALUE = 3\n", encoding="utf-8")
    run_git(git_repo, "add", "src/module.py")
    run_git(git_repo, "commit", "-qm", "new head")
    new_head = run_git(git_repo, "rev-parse", "HEAD")
    run_git(path, "checkout", "-q", "--detach", new_head)
    with pytest.raises(WorktreeError, match="HEAD does not match"):
        manager.cleanup_owned_clean(
            task_id="task-head-mismatch",
            repository=str(git_repo),
            base_commit=base,
            worktree_path=str(path),
        )


def test_ignored_residue_is_dirty_and_cleanup_refuses(worker_config, git_repo):
    (git_repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    run_git(git_repo, "add", ".gitignore")
    run_git(git_repo, "commit", "-qm", "ignore generated residue")
    manager = WorktreeManager(worker_config)
    head = run_git(git_repo, "rev-parse", "HEAD")
    req = manager.validate_request(request(git_repo, head))
    path = manager.prepare(req, "task-ignored")
    (path / "ignored.txt").write_text("must not be silently deleted\n", encoding="utf-8")
    snapshot = manager.snapshot(path)
    assert snapshot.dirty and "ignored.txt" in snapshot.changed_files
    assert snapshot.ignored_files == ("ignored.txt",)
    with pytest.raises(WorktreeDirty):
        manager.cleanup_owned_clean(
            task_id="task-ignored",
            repository=str(git_repo),
            base_commit=head,
            worktree_path=str(path),
        )


def test_snapshot_enforces_changed_file_and_diff_budgets(worker_config, git_repo):
    limited = worker_config.model_copy(
        update={
            "limits": worker_config.limits.model_copy(
                update={
                    "max_changed_files": 1,
                    "max_changed_file_bytes": 1024,
                    "max_diff_bytes": 1024,
                }
            )
        }
    )
    manager = WorktreeManager(limited)
    head = run_git(git_repo, "rev-parse", "HEAD")
    req = manager.validate_request(request(git_repo, head))
    path = manager.prepare(req, "task-resource-count")
    (path / "src" / "one.py").write_text("ONE = 1\n", encoding="utf-8")
    (path / "src" / "two.py").write_text("TWO = 2\n", encoding="utf-8")
    count_snapshot = manager.snapshot(path)
    assert count_snapshot.truncated
    assert "resource-limit" in count_snapshot.changed_files[0]

    path = manager.prepare(req, "task-resource-size")
    (path / "src" / "large.py").write_text("x" * 1025, encoding="utf-8")
    size_snapshot = manager.snapshot(path)
    assert size_snapshot.truncated
    assert "per-file resource limit" in size_snapshot.diff
