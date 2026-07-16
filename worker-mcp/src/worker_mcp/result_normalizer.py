"""Normalize model output against independently measured Git and tool evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .agent_executor import AgentExecution
from .schemas import (
    CommandRecord,
    ResultMetrics,
    ResultStatus,
    TaskResult,
    TestRecord,
)
from .worktree import WorktreeSnapshot


def _relative_files(values: list[Any], worktree: Path) -> list[str]:
    root = worktree.resolve()
    result: set[str] = set()
    for value in values:
        candidate = Path(str(value))
        resolved = candidate.resolve(strict=False) if candidate.is_absolute() else (root / candidate).resolve(strict=False)
        try:
            result.add(resolved.relative_to(root).as_posix())
        except ValueError:
            continue
    return sorted(result)


def _commands(audit: dict[str, Any]) -> list[CommandRecord]:
    records: list[CommandRecord] = []
    for value in audit.get("commands", []):
        if not isinstance(value, dict):
            continue
        try:
            records.append(CommandRecord.model_validate(value))
        except ValueError:
            continue
    return records


def _tests(commands: list[CommandRecord]) -> list[TestRecord]:
    tests: list[TestRecord] = []
    for record in commands:
        command = record.command
        if not any(token in command for token in ("pytest", "compileall", "npm --prefix")):
            continue
        status = "unknown" if record.exit_code is None else ("passed" if record.exit_code == 0 else "failed")
        tests.append(
            TestRecord(command=command, status=status, exit_code=record.exit_code)
        )
    return tests


def normalize_success(
    *,
    task_id: str,
    execution: AgentExecution,
    snapshot: WorktreeSnapshot,
) -> TaskResult:
    reported = execution.reported
    commands = _commands(execution.audit)
    risks = list(reported.risks)
    if snapshot.truncated:
        risks.append("diff exceeded the configured result byte limit and was truncated")
    return TaskResult(
        task_id=task_id,
        status=ResultStatus.SUCCEEDED,
        summary=reported.summary,
        findings=reported.findings,
        files_read=_relative_files(execution.audit.get("files_read", []), snapshot.path),
        files_changed=list(snapshot.changed_files),
        diff=snapshot.diff,
        commands_executed=commands,
        checks_performed=reported.checks_performed,
        tests=_tests(commands),
        acceptance_result=reported.acceptance_result,
        risks=risks,
        unresolved=reported.unresolved,
        artifacts=reported.artifacts,
        metrics=ResultMetrics(
            session_id=execution.session_id,
            turns=execution.turns,
            duration_ms=execution.duration_ms,
        ),
        worktree_path=str(snapshot.path),
    )


def normalize_failure(
    *,
    task_id: str,
    summary: str,
    worktree_path: str | None,
    snapshot: WorktreeSnapshot | None = None,
    partial: bool = False,
    unresolved: list[str] | None = None,
) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        status=ResultStatus.PARTIAL if partial else ResultStatus.FAILED,
        summary=summary,
        files_changed=list(snapshot.changed_files) if snapshot else [],
        diff=snapshot.diff if snapshot else "",
        acceptance_result="not accepted",
        unresolved=unresolved or [summary],
        worktree_path=worktree_path,
    )
