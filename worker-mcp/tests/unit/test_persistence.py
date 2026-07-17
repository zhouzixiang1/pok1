from pathlib import Path

import pytest

from worker_mcp.idempotency import request_fingerprint
from worker_mcp.persistence import ClaimConflict, IdempotencyConflict, Persistence
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
    assert store.path.stat().st_mode & 0o777 == 0o600
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


def test_cancel_and_claim_are_one_atomic_decision(tmp_path: Path):
    store = Persistence(tmp_path / "tasks.sqlite3")
    item = request()
    row, _ = store.create_or_get(item, request_fingerprint(item))
    store.transition(
        row["task_id"],
        TaskStatus.QUEUED,
        phase="queued",
        reason="test",
        progress_summary="queued",
    )
    cancelled, terminal = store.cancel_or_request(
        row["task_id"], pre_execution_result_json="{}"
    )
    assert terminal and cancelled["status"] == TaskStatus.CANCELLED.value
    with pytest.raises(ClaimConflict):
        store.claim_task(row["task_id"], "late-executor")

    active = request("active task").model_copy(
        update={"idempotency_key": "persistence-active-0001"}
    )
    active_row, _ = store.create_or_get(active, request_fingerprint(active))
    store.transition(
        active_row["task_id"],
        TaskStatus.QUEUED,
        phase="queued",
        reason="test",
        progress_summary="queued",
    )
    store.claim_task(active_row["task_id"], "active-executor")
    requested, terminal = store.cancel_or_request(
        active_row["task_id"], pre_execution_result_json="{}"
    )
    assert not terminal
    assert requested["status"] == TaskStatus.PREPARING.value
    assert requested["cancel_requested"] == 1


def test_list_hides_terminal_history_unless_explicitly_requested(tmp_path: Path):
    store = Persistence(tmp_path / "tasks.sqlite3")
    active_request = request("current task").model_copy(
        update={"idempotency_key": "persistence-current-0001"}
    )
    active, _ = store.create_or_get(active_request, request_fingerprint(active_request))
    terminals = []
    for index in range(3):
        terminal_request = request(f"historical task {index}").model_copy(
            update={"idempotency_key": f"persistence-history-000{index + 1}"}
        )
        terminal, _ = store.create_or_get(
            terminal_request, request_fingerprint(terminal_request)
        )
        store.cancel_or_request(
            terminal["task_id"], pre_execution_result_json="{}"
        )
        terminals.append(terminal)

    # Make the ordering contract explicit: the one active row is older than
    # enough newer terminal rows to fill the requested limit before filtering.
    with store._connect() as connection:
        connection.execute(
            "UPDATE tasks SET created_at = ? WHERE task_id = ?",
            ("2026-01-01T00:00:00+00:00", active["task_id"]),
        )
        for index, terminal in enumerate(terminals, start=1):
            connection.execute(
                "UPDATE tasks SET created_at = ? WHERE task_id = ?",
                (f"2026-01-01T00:00:0{index}+00:00", terminal["task_id"]),
            )

    assert [row["task_id"] for row in store.list_tasks(limit=1)] == [
        active["task_id"]
    ]
    assert [
        row["task_id"]
        for row in store.list_tasks(include_terminal=True, limit=1)
    ] == [terminals[-1]["task_id"]]
    assert [
        row["task_id"]
        for row in store.list_tasks(status=TaskStatus.CANCELLED, limit=2)
    ] == [terminals[-1]["task_id"], terminals[-2]["task_id"]]
