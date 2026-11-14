"""The two demo tasks run at the same time, and so do the steps inside each."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from app.execution import execute_tasks
from app.models import Task, TaskStep
from app.seed import DEMO_TASKS


def _task_with_steps(spec: dict) -> tuple[Task, list[TaskStep]]:
    now = datetime.now(UTC)
    task = Task(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title=spec["title"],
        instruction=spec["instruction"],
        status="created",
        created_at=now,
        updated_at=now,
    )
    steps = [
        TaskStep(
            id=uuid.uuid4(),
            task_id=task.id,
            position=position,
            agent_id=agent_id,
            action=action,
            risk=risk,
            status="pending",
            attempt=0,
        )
        for position, (agent_id, action, risk) in enumerate(spec["steps"])
    ]
    return task, steps


@pytest.mark.asyncio
async def test_tasks_and_steps_overlap() -> None:
    running = 0
    peak = 0
    gate = asyncio.Event()

    async def runner(step: TaskStep) -> str:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await gate.wait()
        running -= 1
        step.attempt += 1
        return "ok"

    work = [_task_with_steps(spec) for spec in DEMO_TASKS]
    started = asyncio.create_task(execute_tasks(work, runner))

    # Ten steps across two tasks must all be inside the runner before any finishes.
    while peak < 10:
        await asyncio.sleep(0.01)
    gate.set()

    reports = await started

    assert peak == 10
    assert {report.status for report in reports} == {"completed"}
    assert sorted(step.agent_id for _, steps in work for step in steps) == sorted(
        agent for spec in DEMO_TASKS for agent, _, _ in spec["steps"]
    )
