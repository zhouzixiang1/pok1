from __future__ import annotations

import pytest
from pydantic import ValidationError

from worker_mcp.schemas import ExecutionProfile, TaskEnvelope, TaskType


def envelope(**updates):
    payload = {
        "goal": "inspect source",
        "context": "bounded test",
        "repo": "/tmp/repo",
        "base_commit": "abcdef1",
        "allowed_paths": ["src"],
        "forbidden_paths": ["archive"],
        "constraints": [],
        "acceptance_criteria": ["report evidence"],
        "execution": ExecutionProfile(),
        "idempotency_key": "schema-test-0001",
        "task_type": TaskType.ANALYZE,
    }
    payload.update(updates)
    return TaskEnvelope(**payload)


def test_task_envelope_rejects_extra_fields_and_path_traversal():
    with pytest.raises(ValidationError):
        envelope(unknown=True)
    with pytest.raises(ValidationError):
        envelope(allowed_paths=["../src"])
    with pytest.raises(ValidationError):
        envelope(allowed_paths=[])


def test_write_profile_requires_write_task_type_and_worktree():
    with pytest.raises(ValidationError):
        envelope(execution=ExecutionProfile(read_only=False))
    with pytest.raises(ValidationError):
        ExecutionProfile(use_worktree=False)
    assert envelope(
        execution=ExecutionProfile(read_only=False), task_type=TaskType.PATCH
    ).task_type is TaskType.PATCH
