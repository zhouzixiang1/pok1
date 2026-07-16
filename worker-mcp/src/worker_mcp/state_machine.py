"""Task lifecycle transition contract."""

from __future__ import annotations

from .schemas import TaskStatus


TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
        TaskStatus.NEEDS_REVIEW,
    }
)

NORMAL_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.ACCEPTED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.QUEUED: frozenset({TaskStatus.PREPARING, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.PREPARING: frozenset(
        {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TIMED_OUT, TaskStatus.NEEDS_REVIEW}
    ),
    TaskStatus.RUNNING: frozenset(
        {TaskStatus.VERIFYING, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TIMED_OUT, TaskStatus.NEEDS_REVIEW}
    ),
    TaskStatus.VERIFYING: frozenset(
        {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TIMED_OUT, TaskStatus.NEEDS_REVIEW}
    ),
}

RECOVERY_REQUEUE_FROM = frozenset(
    {TaskStatus.ACCEPTED, TaskStatus.PREPARING, TaskStatus.RUNNING, TaskStatus.VERIFYING}
)


class InvalidTransition(RuntimeError):
    pass


def is_terminal(status: TaskStatus | str) -> bool:
    return TaskStatus(status) in TERMINAL_STATUSES


def validate_transition(
    current: TaskStatus | str,
    target: TaskStatus | str,
    *,
    recovery: bool = False,
) -> None:
    source = TaskStatus(current)
    destination = TaskStatus(target)
    if recovery and destination is TaskStatus.QUEUED and source in RECOVERY_REQUEUE_FROM:
        return
    if destination not in NORMAL_TRANSITIONS.get(source, frozenset()):
        raise InvalidTransition(f"invalid task transition: {source.value} -> {destination.value}")
