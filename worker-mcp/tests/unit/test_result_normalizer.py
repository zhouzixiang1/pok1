from pathlib import Path

from worker_mcp.agent_executor import AgentExecution
from worker_mcp.result_normalizer import normalize_success
from worker_mcp.schemas import WorkerReportedResult
from worker_mcp.worktree import WorktreeSnapshot


def test_normalizer_uses_measured_diff_and_commands(tmp_path: Path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    execution = AgentExecution(
        reported=WorkerReportedResult(
            summary="done", acceptance_result="criteria satisfied"
        ),
        audit={
            "files_read": [str(worktree / "src.py")],
            "commands": [
                {
                    "command": "python -m pytest tests -q",
                    "exit_code": 0,
                    "duration_ms": 12,
                    "allowed": True,
                }
            ],
        },
        session_id="session",
        turns=2,
        duration_ms=20,
    )
    result = normalize_success(
        task_id="task",
        execution=execution,
        snapshot=WorktreeSnapshot(
            path=worktree,
            head="a" * 40,
            changed_files=("src.py",),
            diff="diff --git a/src.py b/src.py",
        ),
    )
    assert result.diff.startswith("diff --git")
    assert result.files_read == ["src.py"]
    assert result.tests[0].status == "passed"
