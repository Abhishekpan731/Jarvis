"""Task execution.

There is no worker process yet (that is Phase 6), so this is the execution
path: it drives a task through the legal state transitions and runs the task's
steps. Steps of one task do not depend on each other, so they run concurrently.
Tasks are independent of each other, so several tasks run concurrently too.

What stays sequential is the state machine itself. CREATED, UNDERSTANDING,
PLANNING, EXECUTING, VERIFYING, and COMPLETED describe one task, and a task
can only be in one of them at a time. Running those transitions in parallel
would be illegal, not faster.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from jarvis_shared.agents import action_allowed
from jarvis_shared.tasks import IllegalTransition, TaskStatus, apply_transition

from app.integrations.runner import IntegrationStepError
from app.models import Task, TaskEvent, TaskStep

# A step runner takes the step and returns its outcome. Tests inject one that
# records timing; the default records that the step finished.
StepRunner = Callable[[TaskStep], Awaitable[str]]


@dataclass
class ExecutionReport:
    task_id: uuid.UUID
    status: str
    step_results: dict[int, str]
    started_at: datetime
    finished_at: datetime

    @property
    def elapsed_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


def _transition(task: Task, target: TaskStatus, actor: str) -> None:
    current = TaskStatus(task.status)
    try:
        apply_transition(current, target)
    except IllegalTransition:
        task.error = f"illegal transition {current.value} -> {target.value}"
        raise
    task.status = target.value
    task.updated_at = datetime.now(UTC)


def record_transition(task: Task, target: TaskStatus, actor: str, reason: str) -> TaskEvent:
    """Move the task and return the event that must be persisted with it."""
    previous = task.status
    _transition(task, target, actor)
    return TaskEvent(
        task_id=task.id,
        from_status=previous,
        to_status=target.value,
        reason=reason,
        actor=actor,
    )


async def _default_runner(step: TaskStep) -> str:
    step.attempt += 1
    step.status = "completed"
    step.updated_at = datetime.now(UTC)
    return f"{step.agent_id}: completed"


async def execute_task(
    task: Task,
    steps: Sequence[TaskStep],
    runner: StepRunner = _default_runner,
) -> ExecutionReport:
    """Run one task. Its steps run at the same time; its states do not.

    A step that raises fails the task. A task with no steps fails too: there is
    nothing to verify, so reporting success would be a lie.
    """
    started = datetime.now(UTC)
    task.heartbeat_at = started

    record_transition(task, TaskStatus.UNDERSTANDING, "executor", "accepted")
    record_transition(task, TaskStatus.PLANNING, "executor", "instruction read")
    record_transition(task, TaskStatus.EXECUTING, "executor", "no high-risk step")

    if not steps:
        task.error = "plan contained no steps"
        record_transition(task, TaskStatus.FAILED, "executor", task.error)
        raise ValueError(task.error)

    async def run_step(step: TaskStep) -> tuple[int, str]:
        if not action_allowed(step.agent_id, step.action):
            step.status = "failed"
            raise RuntimeError("permission_denied")
        if action_requires_approval(step.action):
            step.status = "failed"
            raise RuntimeError("approval_required")
        step.status = "running"
        try:
            outcome = await runner(step)
        except Exception as exc:
            step.status = "failed"
            step.updated_at = datetime.now(UTC)
            raise RuntimeError(f"step {step.position} failed: {exc}") from exc
        step.status = "completed"
        step.updated_at = datetime.now(UTC)
        return step.position, outcome

    try:
        results = dict(await asyncio.gather(*(run_step(step) for step in steps)))
    except Exception as exc:
        task.error = str(exc)
        record_transition(task, TaskStatus.FAILED, "executor", task.error)
        raise

    record_transition(task, TaskStatus.VERIFYING, "executor", "steps finished")
    record_transition(task, TaskStatus.COMPLETED, "executor", "verified")
    task.result = {"steps": {str(position): outcome for position, outcome in results.items()}}

    return ExecutionReport(
        task_id=task.id,
        status=task.status,
        step_results=results,
        started_at=started,
        finished_at=datetime.now(UTC),
    )


class LeaseLost(Exception):
    """Raised when a durable write loses the lease fence."""


_HIGH_RISK_WORDS = ("send", "delete", "pay", "purchase", "deploy", "publish", "transfer")


def action_requires_approval(action: str) -> bool:
    text = action.lower()
    return any(word in text for word in _HIGH_RISK_WORDS)


async def _resume_to_executing(task: Task, persist, still_owner) -> None:
    """Walk the legal path to executing, or finish verifying. Never assume created."""
    status = TaskStatus(task.status)
    if status == TaskStatus.WAITING_FOR_APPROVAL:
        raise RuntimeError("approval_required")
    if status in (TaskStatus.WAITING_FOR_USER, TaskStatus.PAUSED):
        raise RuntimeError("not_runnable")
    if status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED):
        return
    if status == TaskStatus.VERIFYING:
        return
    if status == TaskStatus.EXECUTING:
        return

    path: list[TaskStatus]
    if status == TaskStatus.CREATED:
        path = [TaskStatus.UNDERSTANDING, TaskStatus.PLANNING, TaskStatus.EXECUTING]
    elif status == TaskStatus.UNDERSTANDING:
        path = [TaskStatus.PLANNING, TaskStatus.EXECUTING]
    elif status == TaskStatus.PLANNING or status == TaskStatus.REQUIRES_RETRY:
        path = [TaskStatus.EXECUTING]
    elif status == TaskStatus.FAILED:
        path = [TaskStatus.REQUIRES_RETRY, TaskStatus.EXECUTING]
    else:
        raise RuntimeError("not_runnable")

    for target in path:
        if not await still_owner():
            raise LeaseLost
        await persist(task, target, "worker", "durable resume")
        task.status = target.value


async def execute_task_durable(
    task: Task,
    steps: Sequence[TaskStep],
    *,
    persist,
    still_owner,
    mark_step,
    runner: StepRunner | None = None,
) -> ExecutionReport:
    """Run a claimed task. The runner is not given a database session.

    ``persist``, ``still_owner``, and ``mark_step`` each open, commit, and close
    their own session. A step is marked running before the runner and completed
    only after the runner returns. Completed steps are not run again.
    """
    started = datetime.now(UTC)
    if not await still_owner():
        raise LeaseLost

    await _resume_to_executing(task, persist, still_owner)
    if task.status in (TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value):
        return ExecutionReport(task.id, task.status, {}, started, datetime.now(UTC))
    if task.status == TaskStatus.FAILED.value:
        raise RuntimeError("not_runnable")

    if task.status == TaskStatus.VERIFYING.value:
        if steps and all(step.status == "completed" for step in steps):
            await persist(task, TaskStatus.COMPLETED, "worker", "verified")
            task.status = TaskStatus.COMPLETED.value
            return ExecutionReport(task.id, task.status, {}, started, datetime.now(UTC))
        await persist(task, TaskStatus.FAILED, "worker", "verification failed")
        task.status = TaskStatus.FAILED.value
        raise RuntimeError("validation_error")

    if not steps:
        await persist(task, TaskStatus.FAILED, "worker", "empty_plan")
        task.status = TaskStatus.FAILED.value
        raise RuntimeError("empty_plan")

    results: dict[int, str] = {}
    for step in sorted(steps, key=lambda item: item.position):
        if step.status == "completed":
            results[step.position] = "skipped"
            continue
        if not action_allowed(step.agent_id, step.action):
            await mark_step(step, "failed")
            await persist(task, TaskStatus.FAILED, "worker", "permission_denied")
            task.status = TaskStatus.FAILED.value
            raise RuntimeError("permission_denied")
        if action_requires_approval(step.action):
            await mark_step(step, "failed")
            await persist(task, TaskStatus.FAILED, "worker", "approval_required")
            task.status = TaskStatus.FAILED.value
            raise RuntimeError("approval_required")
        if not await still_owner():
            raise LeaseLost
        await mark_step(step, "running")
        step.status = "running"
        try:
            if runner is None:
                outcome = f"{step.agent_id}: completed"
            else:
                outcome = await runner(step)
        except IntegrationStepError as exc:
            await mark_step(step, "failed")
            step.status = "failed"
            await persist(task, TaskStatus.FAILED, "worker", str(exc))
            task.status = TaskStatus.FAILED.value
            raise
        except Exception as exc:
            await mark_step(step, "failed")
            step.status = "failed"
            await persist(task, TaskStatus.FAILED, "worker", "step_failed")
            task.status = TaskStatus.FAILED.value
            raise RuntimeError("step_failed") from exc
        if not await still_owner():
            raise LeaseLost
        await mark_step(step, "completed")
        step.status = "completed"
        results[step.position] = outcome

    if not await still_owner():
        raise LeaseLost
    await persist(task, TaskStatus.VERIFYING, "worker", "steps finished")
    task.status = TaskStatus.VERIFYING.value
    await persist(task, TaskStatus.COMPLETED, "worker", "verified")
    task.status = TaskStatus.COMPLETED.value
    task.result = {"steps": {str(position): outcome for position, outcome in results.items()}}
    return ExecutionReport(
        task_id=task.id,
        status=task.status,
        step_results=results,
        started_at=started,
        finished_at=datetime.now(UTC),
    )


async def execute_tasks(
    work: Sequence[tuple[Task, Sequence[TaskStep]]],
    runner: StepRunner = _default_runner,
) -> list[ExecutionReport]:
    """Run several tasks at once. Each task still owns its own state machine."""
    reports = await asyncio.gather(*(execute_task(task, steps, runner) for task, steps in work))
    return list(reports)
