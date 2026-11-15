"""Create the two demo tasks and run them together.

Agents come from the migration, not from here. This only inserts the tasks a
signed-in user owns and hands them to the existing executor.
"""

import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution import execute_tasks
from app.models import Agent, Task, TaskEvent, TaskStep, User

# Two tasks, five steps each, covering all ten agents. Steps within a task are
# independent, which is why the executor runs them concurrently.
DEMO_TASKS = (
    {
        "title": "Morning sweep",
        "instruction": "Prepare the morning sweep from connected data only.",
        "steps": (
            ("briefing", "compile today's briefing", "low"),
            ("notes", "retrieve notes flagged for today", "low"),
            ("contacts", "look up people mentioned in today's notes", "low"),
            ("travel", "draft today's travel plan", "low"),
            ("news", "collect headlines for followed topics", "low"),
        ),
    },
    {
        "title": "Personal review",
        "instruction": "Review personal routines. Do not give advice or spend money.",
        "steps": (
            ("health", "summarize tracked habits", "low"),
            ("shopping", "compare items on the open list", "low"),
            ("learning", "suggest the next topic to review", "low"),
            ("home", "list chores due today", "low"),
            ("wellness", "plan breaks around today's schedule", "low"),
        ),
    },
)


async def create_demo_tasks(session: AsyncSession, user_id: uuid.UUID) -> list[Task]:
    """Insert the two demo tasks and their steps. Does not run them."""
    agent_ids = set((await session.execute(select(Agent.id))).scalars())
    missing = {step[0] for task in DEMO_TASKS for step in task["steps"]} - agent_ids
    if missing:
        raise RuntimeError(f"Agents not seeded, run migrations first: {sorted(missing)}")

    created: list[Task] = []
    now = datetime.now(UTC)
    for spec in DEMO_TASKS:
        task = Task(
            id=uuid.uuid4(),
            user_id=user_id,
            title=spec["title"],
            instruction=spec["instruction"],
            status="created",
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        session.add(
            TaskEvent(
                task_id=task.id,
                from_status=None,
                to_status="created",
                reason="demo task created",
                actor="seed",
            )
        )
        for position, (agent_id, action, risk) in enumerate(spec["steps"]):
            session.add(
                TaskStep(
                    task_id=task.id,
                    position=position,
                    agent_id=agent_id,
                    action=action,
                    risk=risk,
                    status="pending",
                    attempt=0,
                )
            )
        created.append(task)
    await session.commit()
    return created


async def run_demo_tasks(session: AsyncSession, user: User) -> None:
    tasks = await create_demo_tasks(session, user.id)
    loaded = []
    for task in tasks:
        steps = (
            (
                await session.execute(
                    select(TaskStep).where(TaskStep.task_id == task.id).order_by(TaskStep.position)
                )
            )
            .scalars()
            .all()
        )
        loaded.append((task, steps))
    reports = await execute_tasks(loaded)
    await session.commit()
    for report in reports:
        print(
            f"{report.task_id} {report.status} "
            f"steps={sorted(report.step_results)} elapsed={report.elapsed_seconds:.3f}s"
        )


async def _main() -> None:
    from app.db import session_scope, set_tenant_guc

    async with session_scope() as session:
        user = (await session.execute(select(User).limit(1))).scalar_one()
        await set_tenant_guc(session, user.id)
        await run_demo_tasks(session, user)


if __name__ == "__main__":
    asyncio.run(_main())
