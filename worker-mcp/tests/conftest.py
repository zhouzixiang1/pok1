from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from worker_mcp.config import WorkerConfig


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "Worker Test", "GIT_AUTHOR_EMAIL": "worker@test.invalid", "GIT_COMMITTER_NAME": "Worker Test", "GIT_COMMITTER_EMAIL": "worker@test.invalid"},
    )
    return result.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q")
    run_git(repo, "config", "user.name", "Worker Test")
    run_git(repo, "config", "user.email", "worker@test.invalid")
    (repo / "src").mkdir()
    (repo / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_module.py").write_text(
        "from src.module import VALUE\n\ndef test_value(): assert VALUE == 1\n",
        encoding="utf-8",
    )
    (repo / "archive").mkdir()
    (repo / "archive" / "retired.py").write_text("SECRET_HISTORY = True\n", encoding="utf-8")
    run_git(repo, "add", "src", "tests", "archive")
    run_git(repo, "commit", "-qm", "base")
    return repo


@pytest.fixture
def worker_config(tmp_path: Path, git_repo: Path) -> WorkerConfig:
    config = WorkerConfig.model_validate(
        {
            "schema_version": 1,
            "state_dir": tmp_path / "state",
            "worktree_root": tmp_path / "state" / "worktrees",
            "allowed_repositories": [git_repo],
            "mandatory_forbidden_paths": ["archive", ".git", ".env"],
            "gateway": {
                "endpoint": "http://127.0.0.1:15721",
                "health_path": "/health",
                "auth_token_env": "WORKER_MCP_TEST_TOKEN",
                "require_auth_token": False,
            },
            "runtime": {
                "backend": "mock",
                "python_executable": sys.executable,
                "expected_claude_agent_sdk": "0.2.91",
                "expected_claude_code": "2.1.205",
                "expected_cc_switch": "3.17.0",
            },
            "limits": {
                "global_read_tasks": 2,
                "repository_read_tasks": 2,
                "global_write_tasks": 1,
                "repository_write_tasks": 1,
                "max_subprocesses": 2,
                "max_task_timeout_sec": 120,
                "max_turns": 10,
                "read_retry_count": 1,
            },
        }
    )
    config.prepare_directories()
    return config
