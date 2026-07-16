from pathlib import Path

import pytest

from worker_mcp.idempotency import request_fingerprint
from worker_mcp.persistence import IdempotencyConflict, Persistence
from worker_mcp.schemas import ExecutionProfile, TaskEnvelope, TaskStatus


def request(goal="inspect"):
    return TaskEnvelope(
        goal=goal,
        context="",
        repo="/tmp/repo",
        base_commit="abcdef1",
        allowed_paths=["src"],
        forbidden_paths=[],
        constraints=[],
        acceptance_criteria=[],
        execution=ExecutionProfile(),
        idempotency_key="persistence-test-0001",
    )


def test_persistence_idempotency_and_history(tmp_path: Path):
    store = Persistence(tmp_path / "tasks.sqlite3")
    item = request()
    first, replay = store.create_or_get(item, request_fingerprint(item))
    assert not replay
    second, replay = store.create_or_get(item, request_fingerprint(item))
    assert replay and second["task_id"] == first["task_id"]
    store.transition(
        first["task_id"],
        TaskStatus.QUEUED,
        phase="queued",
        reason="test",
        progress_summary="queued",
    )
    assert [row["to_status"] for row in store.transitions(first["task_id"])] == [
        "accepted",
        "queued",
    ]
    reopened = Persistence(tmp_path / "tasks.sqlite3")
    assert reopened.get_task(first["task_id"])["status"] == "queued"


def test_same_key_different_fingerprint_conflicts(tmp_path: Path):
    store = Persistence(tmp_path / "tasks.sqlite3")
    first = request()
    store.create_or_get(first, request_fingerprint(first))
    changed = request("different")
    with pytest.raises(IdempotencyConflict):
        store.create_or_get(changed, request_fingerprint(changed))
