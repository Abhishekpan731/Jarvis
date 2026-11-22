"""The task state machine.

This module is pure: it decides whether a transition is legal and nothing else.
Persisting the transition is the caller's job, and the caller must record a
task event in the same transaction. A state change without an event is a bug.
"""

from enum import StrEnum


class TaskStatus(StrEnum):
    CREATED = "created"
    UNDERSTANDING = "understanding"
    PLANNING = "planning"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    WAITING_FOR_USER = "waiting_for_user"
    REQUIRES_RETRY = "requires_retry"


class IllegalTransition(Exception):
    """Raised when a task is asked to move to a state it cannot reach."""

    def __init__(self, current: TaskStatus, target: TaskStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Cannot move a task from {current.value} to {target.value}.")


# The happy path runs top to bottom. Everything else is recovery or control.
_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.UNDERSTANDING, TaskStatus.CANCELLED}),
    TaskStatus.UNDERSTANDING: frozenset(
        {TaskStatus.PLANNING, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.PLANNING: frozenset(
        {
            TaskStatus.WAITING_FOR_APPROVAL,
            TaskStatus.EXECUTING,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_FOR_APPROVAL: frozenset(
        {TaskStatus.EXECUTING, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.EXECUTING: frozenset(
        {
            TaskStatus.VERIFYING,
            TaskStatus.WAITING_FOR_USER,
            TaskStatus.PAUSED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.WAITING_FOR_USER: frozenset({TaskStatus.EXECUTING, TaskStatus.CANCELLED}),
    TaskStatus.PAUSED: frozenset({TaskStatus.EXECUTING, TaskStatus.CANCELLED}),
    TaskStatus.VERIFYING: frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED}),
    TaskStatus.FAILED: frozenset({TaskStatus.REQUIRES_RETRY}),
    TaskStatus.REQUIRES_RETRY: frozenset({TaskStatus.EXECUTING, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

TERMINAL = frozenset({TaskStatus.COMPLETED, TaskStatus.CANCELLED})


def allowed_transitions(status: TaskStatus) -> frozenset[TaskStatus]:
    return _TRANSITIONS[status]


def apply_transition(current: TaskStatus, target: TaskStatus) -> TaskStatus:
    """Return the new status, or raise if the move is not legal."""
    if target not in _TRANSITIONS[current]:
        raise IllegalTransition(current, target)
    return target
