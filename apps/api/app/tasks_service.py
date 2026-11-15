"""Create durable tasks from a validated plan.

The API enqueues work. It does not run it. A failed transaction leaves neither
a task nor a job. High-risk plans are stored as waiting_for_approval and are
not enqueued.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.schemas import Plan
from app.jobs import insert_job
from app.models import Task, TaskEvent, TaskStep


async def create_task_from_plan(
    session: AsyncSession,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    plan: Plan,
    *,
    enqueue: bool = True,
    commit: bool = True,
) -> uuid.UUID:
    """Persist a task, its steps, and (unless approval is required) one job.

    Commits before returning unless the caller still has rows to attach.
    Does not execute the task.
    """
    now = datetime.now(UTC)
    waiting = plan.requires_approval
    task = Task(
        id=uuid.uuid4(),
        user_id=user_id,
        conversation_id=conversation_id,
        title=plan.intent[:200],
        instruction=plan.intent,
        status="waiting_for_approval" if waiting else "created",
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    session.add(
        TaskEvent(
            task_id=task.id,
            from_status=None,
            to_status=task.status,
            reason="planned by supervisor",
            actor="supervisor",
        )
    )
    steps = [
        TaskStep(
            id=uuid.uuid4(),
            task_id=task.id,
            position=position,
            agent_id=step.agent,
            action=step.action,
            risk=plan.risk,
            status="pending",
            attempt=0,
        )
        for position, step in enumerate(plan.steps)
    ]
    session.add_all(steps)
    await session.flush()
    if enqueue and not waiting:
        await insert_job(session, user_id=user_id, task_id=task.id)
    if commit:
        await session.commit()
    return task.id


async def load_task_for_worker(
    session: AsyncSession, task_id: uuid.UUID
) -> tuple[Task, list[TaskStep]]:
    task = (
        await session.execute(text("SELECT * FROM tasks WHERE id = :id"), {"id": task_id})
    ).mappings().one()
    steps = (
        await session.execute(
            text("SELECT * FROM task_steps WHERE task_id = :id ORDER BY position"),
            {"id": task_id},
        )
    ).mappings().all()
    task_obj = await session.get(Task, task_id)
    step_objs = []
    for row in steps:
        step = await session.get(TaskStep, row["id"])
        step_objs.append(step)
    if task_obj is None:
        raise LookupError("task missing")
    # Touch the mapping so a missing row is obvious to callers.
    _ = task["id"]
    return task_obj, step_objs
