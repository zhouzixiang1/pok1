import pytest

from worker_mcp.schemas import TaskStatus
from worker_mcp.state_machine import InvalidTransition, is_terminal, validate_transition


def test_happy_path_and_terminal_states():
    path = [
        TaskStatus.ACCEPTED,
        TaskStatus.QUEUED,
        TaskStatus.PREPARING,
        TaskStatus.RUNNING,
        TaskStatus.VERIFYING,
        TaskStatus.SUCCEEDED,
    ]
    for source, target in zip(path, path[1:]):
        validate_transition(source, target)
    assert is_terminal(TaskStatus.SUCCEEDED)
    assert is_terminal(TaskStatus.NEEDS_REVIEW)


def test_illegal_and_recovery_transitions():
    with pytest.raises(InvalidTransition):
        validate_transition(TaskStatus.RUNNING, TaskStatus.SUCCEEDED)
    with pytest.raises(InvalidTransition):
        validate_transition(TaskStatus.RUNNING, TaskStatus.QUEUED)
    validate_transition(TaskStatus.RUNNING, TaskStatus.QUEUED, recovery=True)

    for status in (TaskStatus.ACCEPTED, TaskStatus.QUEUED):
        with pytest.raises(InvalidTransition):
            validate_transition(status, TaskStatus.NEEDS_REVIEW)
        validate_transition(
            status,
            TaskStatus.NEEDS_REVIEW,
            recovery=True,
        )
