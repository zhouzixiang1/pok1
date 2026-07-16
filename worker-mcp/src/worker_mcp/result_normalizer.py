"""Normalize model output against independently measured Git and tool evidence."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

from .agent_executor import AgentExecution
from .schemas import (
    CommandRecord,
    ResultMetrics,
    ResultStatus,
    TaskResult,
    TestRecord,
    WorkerReportedResult,
)
from .worktree import WorktreeSnapshot


_REDACTED = "[REDACTED]"
_SENSITIVE_ENV_NAME = re.compile(
    r"(?:^|_)(?:AUTH(?:ORIZATION)?|TOKEN|SECRET|PASSWORD|PASSWD|"
    r"API_?KEY|CRED(?:ENTIALS?)?|PRIVATE_?KEY)(?:$|_)",
    re.IGNORECASE,
)
_AUTHORIZATION = re.compile(
    r"(?i)(?P<prefix>\bauthorization\s*[:=]\s*bearer\s+)"
    r"(?P<quote>['\"]?)(?P<value>[^\s,'\";}\]]+)(?P=quote)"
)
_BEARER = re.compile(
    r"(?i)(?P<prefix>\bbearer\s+)(?P<quote>['\"]?)"
    r"(?P<value>[A-Za-z0-9._~+/=-]{4,})(?P=quote)"
)
_NAMED_QUOTED_SECRET = re.compile(
    r"(?ix)"
    r"(?P<prefix>['\"]?[A-Z0-9_.-]*"
    r"(?:AUTH[_-]?TOKEN|TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|"
    r"CREDENTIAL|PRIVATE[_-]?KEY)"
    r"[A-Z0-9_.-]*['\"]?\s*[:=]\s*)"
    r"(?P<quote>['\"])(?P<value>[^\r\n]*?)(?P=quote)"
)
_NAMED_BARE_SECRET = re.compile(
    r"(?ix)"
    r"(?P<prefix>['\"]?[A-Z0-9_.-]*"
    r"(?:AUTH[_-]?TOKEN|TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|"
    r"CREDENTIAL|PRIVATE[_-]?KEY)"
    r"[A-Z0-9_.-]*['\"]?\s*[:=]\s*)"
    r"(?!['\"])(?P<value>[^\s,;}\]]+)"
)
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_OPAQUE_TOKEN_PATTERNS = (
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{12,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
)


def _secret_values(extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    values = {value for value in extra if len(value) >= 4}
    values.update(
        value
        for name, value in os.environ.items()
        if len(value) >= 4 and _SENSITIVE_ENV_NAME.search(name)
    )
    return tuple(sorted(values, key=len, reverse=True))


def redact_sensitive_text(value: str, *, secrets: tuple[str, ...] = ()) -> str:
    """Redact known credentials and common token-shaped values from output."""

    result = str(value)
    for secret in _secret_values(secrets):
        result = result.replace(secret, _REDACTED)
    result = _PEM_PRIVATE_KEY.sub(_REDACTED, result)
    result = _AUTHORIZATION.sub(
        lambda match: (
            match.group("prefix")
            + match.group("quote")
            + _REDACTED
            + match.group("quote")
        ),
        result,
    )
    result = _BEARER.sub(
        lambda match: (
            match.group("prefix")
            + match.group("quote")
            + _REDACTED
            + match.group("quote")
        ),
        result,
    )
    result = _NAMED_QUOTED_SECRET.sub(
        lambda match: (
            match.group("prefix")
            + match.group("quote")
            + _REDACTED
            + match.group("quote")
        ),
        result,
    )
    result = _NAMED_BARE_SECRET.sub(
        lambda match: match.group("prefix") + _REDACTED,
        result,
    )
    for pattern in _OPAQUE_TOKEN_PATTERNS:
        result = pattern.sub(_REDACTED, result)
    return result


def _redact_reported(
    reported: WorkerReportedResult, *, secrets: tuple[str, ...]
) -> WorkerReportedResult:
    payload = reported.model_dump(mode="python")
    for key in ("summary", "acceptance_result"):
        payload[key] = redact_sensitive_text(payload[key], secrets=secrets)
    for key in ("risks", "unresolved"):
        payload[key] = [
            redact_sensitive_text(value, secrets=secrets)
            for value in payload[key]
        ]
    for finding in payload["findings"]:
        for key in ("message", "file", "symbol", "evidence"):
            if finding[key] is not None:
                finding[key] = redact_sensitive_text(
                    finding[key], secrets=secrets
                )
    for check in payload["checks_performed"]:
        for key in ("name", "details"):
            check[key] = redact_sensitive_text(check[key], secrets=secrets)
    for artifact in payload["artifacts"]:
        for key in ("kind", "path", "digest"):
            if artifact[key] is not None:
                artifact[key] = redact_sensitive_text(
                    artifact[key], secrets=secrets
                )
    return WorkerReportedResult.model_validate(payload)


def _relative_files(
    values: list[Any], worktree: Path, *, secrets: tuple[str, ...]
) -> list[str]:
    root = worktree.resolve()
    result: set[str] = set()
    for value in values:
        candidate = Path(str(value))
        resolved = candidate.resolve(strict=False) if candidate.is_absolute() else (root / candidate).resolve(strict=False)
        try:
            result.add(
                redact_sensitive_text(
                    resolved.relative_to(root).as_posix(), secrets=secrets
                )
            )
        except ValueError:
            continue
    return sorted(result)


def _commands(
    audit: dict[str, Any], *, secrets: tuple[str, ...]
) -> list[CommandRecord]:
    records: list[CommandRecord] = []
    for value in audit.get("commands", []):
        if not isinstance(value, dict):
            continue
        try:
            record = CommandRecord.model_validate(value)
            records.append(
                record.model_copy(
                    update={
                        "command": redact_sensitive_text(
                            record.command, secrets=secrets
                        )
                    }
                )
            )
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
    secrets: tuple[str, ...] = (),
) -> TaskResult:
    secrets = tuple(
        sorted(
            set(execution.redaction_secrets) | set(secrets),
            key=len,
            reverse=True,
        )
    )
    reported = _redact_reported(execution.reported, secrets=secrets)
    commands = _commands(execution.audit, secrets=secrets)
    risks = list(reported.risks)
    if snapshot.truncated:
        risks.append("diff exceeded the configured result byte limit and was truncated")
    return TaskResult(
        task_id=task_id,
        status=ResultStatus.SUCCEEDED,
        summary=reported.summary,
        findings=reported.findings,
        files_read=_relative_files(
            execution.audit.get("files_read", []),
            snapshot.path,
            secrets=secrets,
        ),
        files_changed=[
            redact_sensitive_text(value, secrets=secrets)
            for value in snapshot.changed_files
        ],
        diff=redact_sensitive_text(snapshot.diff, secrets=secrets),
        commands_executed=commands,
        checks_performed=reported.checks_performed,
        tests=_tests(commands),
        acceptance_result=reported.acceptance_result,
        risks=risks,
        unresolved=reported.unresolved,
        artifacts=reported.artifacts,
        metrics=ResultMetrics(
            session_id=redact_sensitive_text(
                execution.session_id, secrets=secrets
            ),
            turns=execution.turns,
            duration_ms=execution.duration_ms,
        ),
        worktree_path=redact_sensitive_text(str(snapshot.path), secrets=secrets),
    )


def normalize_failure(
    *,
    task_id: str,
    summary: str,
    worktree_path: str | None,
    snapshot: WorktreeSnapshot | None = None,
    partial: bool = False,
    unresolved: list[str] | None = None,
    secrets: tuple[str, ...] = (),
) -> TaskResult:
    safe_summary = redact_sensitive_text(summary, secrets=secrets)
    return TaskResult(
        task_id=task_id,
        status=ResultStatus.PARTIAL if partial else ResultStatus.FAILED,
        summary=safe_summary,
        files_changed=(
            [
                redact_sensitive_text(value, secrets=secrets)
                for value in snapshot.changed_files
            ]
            if snapshot
            else []
        ),
        diff=(
            redact_sensitive_text(snapshot.diff, secrets=secrets)
            if snapshot
            else ""
        ),
        acceptance_result="not accepted",
        unresolved=[
            redact_sensitive_text(value, secrets=secrets)
            for value in (unresolved or [safe_summary])
        ],
        worktree_path=(
            redact_sensitive_text(worktree_path, secrets=secrets)
            if worktree_path
            else None
        ),
    )
