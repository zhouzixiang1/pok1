"""Strict public MCP and internal Worker result schemas."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TaskStatus(StrEnum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    NEEDS_REVIEW = "needs_review"


class ResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class TaskType(StrEnum):
    ANALYZE = "analyze"
    REVIEW = "review"
    PATCH = "patch"
    TEST = "test"
    REFACTOR = "refactor"
    DOCUMENT = "document"


class ExecutionProfile(StrictModel):
    read_only: bool = True
    use_worktree: bool = True
    max_turns: int = Field(default=20, ge=1, le=100)
    timeout_sec: int = Field(default=1800, ge=5, le=7200)

    @model_validator(mode="after")
    def require_worktree(self) -> "ExecutionProfile":
        if not self.use_worktree:
            raise ValueError("all Worker tasks must use an isolated worktree")
        return self


def _validate_relative_path(value: str) -> str:
    text = value.strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or text == "." or path.is_absolute():
        raise ValueError("scope paths must be non-empty repository-relative paths")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("scope paths must not contain dot traversal")
    return path.as_posix().rstrip("/")


class TaskEnvelope(StrictModel):
    goal: str = Field(min_length=1, max_length=20_000)
    context: str = Field(default="", max_length=100_000)
    repo: str = Field(min_length=1, max_length=4096)
    base_commit: str = Field(min_length=7, max_length=128)
    allowed_paths: list[str] = Field(min_length=1, max_length=256)
    forbidden_paths: list[str] = Field(default_factory=list, max_length=256)
    constraints: list[str] = Field(default_factory=list, max_length=128)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=128)
    execution: ExecutionProfile = Field(default_factory=ExecutionProfile)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{7,255}$")
    trace_id: str | None = Field(default=None, min_length=1, max_length=256)
    task_type: TaskType = TaskType.ANALYZE

    @field_validator("allowed_paths", "forbidden_paths")
    @classmethod
    def validate_paths(cls, values: list[str]) -> list[str]:
        normalized = [_validate_relative_path(value) for value in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("scope paths must be unique")
        return normalized

    @field_validator("constraints", "acceptance_criteria")
    @classmethod
    def validate_text_lists(cls, values: list[str]) -> list[str]:
        cleaned = [" ".join(value.split()) for value in values]
        if any(not value for value in cleaned):
            raise ValueError("constraint and acceptance entries must be non-empty")
        return cleaned

    @model_validator(mode="after")
    def validate_write_scope(self) -> "TaskEnvelope":
        if not self.execution.read_only and self.task_type in {
            TaskType.ANALYZE,
            TaskType.REVIEW,
        }:
            raise ValueError("write execution requires a write-capable task_type")
        return self


class SubmitResponse(StrictModel):
    task_id: str
    status: TaskStatus
    idempotent_replay: bool = False


class StatusResponse(StrictModel):
    task_id: str
    status: TaskStatus
    phase: str
    attempt: int = Field(ge=0)
    updated_at: datetime
    progress_summary: str
    worktree_path: str | None = None


class Finding(StrictModel):
    severity: Literal["info", "low", "medium", "high", "critical"] = "info"
    message: str
    file: str | None = None
    symbol: str | None = None
    evidence: str | None = None


class CommandRecord(StrictModel):
    command: str
    exit_code: int | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    allowed: bool = True


class CheckRecord(StrictModel):
    name: str
    status: Literal["passed", "failed", "skipped"]
    details: str = ""


class TestRecord(StrictModel):
    command: str
    status: Literal["passed", "failed", "unknown"]
    exit_code: int | None = None


class ArtifactRecord(StrictModel):
    kind: str
    path: str
    digest: str | None = None


class ResultMetrics(StrictModel):
    session_id: str = ""
    turns: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)


class WorkerReportedResult(StrictModel):
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    checks_performed: list[CheckRecord] = Field(default_factory=list)
    acceptance_result: str
    risks: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactRecord] = Field(default_factory=list)


class TaskResult(StrictModel):
    task_id: str
    status: ResultStatus
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    files_read: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    diff: str = ""
    commands_executed: list[CommandRecord] = Field(default_factory=list)
    checks_performed: list[CheckRecord] = Field(default_factory=list)
    tests: list[TestRecord] = Field(default_factory=list)
    acceptance_result: str = ""
    risks: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    metrics: ResultMetrics = Field(default_factory=ResultMetrics)
    worktree_path: str | None = None


class CancelResponse(StrictModel):
    task_id: str
    status: TaskStatus
    worktree_path: str | None = None
    progress_summary: str


class ListTasksRequest(StrictModel):
    status: TaskStatus | None = None
    include_terminal: bool = False
    repo: str | None = None
    task_type: TaskType | None = None
    since: datetime | None = None
    limit: int = Field(default=50, ge=1, le=500)


class TaskSummary(StrictModel):
    task_id: str
    status: TaskStatus
    task_type: TaskType
    repo: str
    goal: str
    attempt: int
    created_at: datetime
    updated_at: datetime


class ListTasksResponse(StrictModel):
    tasks: list[TaskSummary]


class ComponentHealth(StrictModel):
    status: Literal["healthy", "degraded", "unhealthy", "skipped"]
    detail: str = ""


class HealthResponse(StrictModel):
    status: Literal["healthy", "degraded", "unhealthy"]
    components: dict[str, ComponentHealth]
    checked_at: datetime


def worker_output_json_schema() -> dict[str, Any]:
    return WorkerReportedResult.model_json_schema()
