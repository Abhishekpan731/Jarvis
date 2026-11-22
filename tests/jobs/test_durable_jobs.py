"""Durable queue behavior. These tests use the real database and the claim SQL."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db import session_scope, set_tenant_guc, worker_session_scope
from app.execution import execute_task_durable, record_transition
from app.jobs import (
    LeaseLost,
    claim_one,
    complete_job,
    fail_job,
    heartbeat,
    insert_job,
    reap_expired,
)
from app.models import Task, TaskEvent, TaskStep, User
from jarvis_shared.tasks import IllegalTransition, TaskStatus, apply_transition
from sqlalchemy import text


async def _user() -> uuid.UUID:
    user_id = uuid.uuid4()
    async with session_scope() as session:
        session.add(
            User(
                id=user_id,
                email=f"{user_id}@example.com",
                password_hash="x",
                display_name="t",
            )
        )
        await session.commit()
    return user_id


async def _task(user_id: uuid.UUID, status: str = "created") -> uuid.UUID:
    task_id = uuid.uuid4()
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        session.add(
            Task(
                id=task_id,
                user_id=user_id,
                title="t",
                instruction="t",
                status=status,
            )
        )
        session.add(
            TaskEvent(task_id=task_id, from_status=None, to_status=status, reason="test", actor="test")
        )
        session.add(
            TaskStep(
                id=uuid.uuid4(),
                task_id=task_id,
                position=0,
                agent_id="research",
                action="compare",
                risk="low",
                status="pending",
                attempt=0,
            )
        )
        await session.commit()
    return task_id


async def _isolate(user_ids: list[uuid.UUID]) -> None:
    """Hide leftover jobs from earlier failed runs so this test owns the queue."""
    async with worker_session_scope() as session:
        await session.execute(
            text(
                """
                UPDATE jobs
                SET available_at = now() + interval '1 day'
                WHERE user_id <> ALL(:ids)
                  AND status = 'queued'
                """
            ),
            {"ids": user_ids},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_two_workers_claim_distinct_jobs():
    user_a, user_b = await _user(), await _user()
    await _isolate([user_a, user_b])
    await insert_ready(user_a)
    await insert_ready(user_b)
    async with worker_session_scope() as left, worker_session_scope() as right:
        first = asyncio.create_task(claim_one(left, worker_id="w1"))
        second = asyncio.create_task(claim_one(right, worker_id="w2"))
        claimed = [job for job in await asyncio.gather(first, second) if job]
    assert len(claimed) == 2
    assert {job["id"] for job in claimed}.__len__() == 2
    assert {job["user_id"] for job in claimed} == {user_a, user_b}


@pytest.mark.asyncio
async def test_one_user_is_not_claimed_twice_while_running():
    user_id = await _user()
    await _isolate([user_id])
    await insert_ready(user_id)
    await insert_ready(user_id)
    async with worker_session_scope() as session:
        first = await claim_one(session, worker_id="w1", per_user_max=1)
    async with worker_session_scope() as session:
        second = await claim_one(session, worker_id="w2", per_user_max=1)
    assert first is not None
    assert second is None
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        statuses = (
            await session.execute(
                text("SELECT status FROM jobs WHERE user_id = :id ORDER BY created_at"),
                {"id": user_id},
            )
        ).scalars().all()
    assert statuses.count("running") == 1
    assert statuses.count("queued") == 1


@pytest.mark.asyncio
async def test_stale_token_cannot_complete_or_heartbeat():
    user_id = await _user()
    await _isolate([user_id])
    job_id = await insert_ready(user_id)
    async with worker_session_scope() as session:
        claimed = await claim_one(session, worker_id="owner")
    assert claimed is not None
    stale = uuid.uuid4()
    async with worker_session_scope() as session:
        alive = await heartbeat(session, claimed["id"], stale)
    assert alive is False
    with pytest.raises(LeaseLost):
        async with worker_session_scope() as session:
            await complete_job(session, claimed["id"], stale)
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        status = (
            await session.execute(text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id})
        ).scalar_one()
    assert status == "running"


@pytest.mark.asyncio
async def test_expired_lease_is_reaped_and_retried():
    user_id = await _user()
    await _isolate([user_id])
    job_id = await insert_ready(user_id)
    async with worker_session_scope() as session:
        claimed = await claim_one(session, worker_id="dead", lease_seconds=60)
    assert claimed is not None
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        await session.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": job_id},
        )
        await session.commit()
    async with worker_session_scope() as session:
        recovered = await reap_expired(session, only_id=job_id)
    assert recovered == 1
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        row = (
            await session.execute(
                text("SELECT status, lease_token, last_error FROM jobs WHERE id = :id"),
                {"id": job_id},
            )
        ).one()
    assert row.status == "retry_wait"
    assert row.lease_token is None
    assert row.last_error == "lease_expired"


@pytest.mark.asyncio
async def test_permanent_error_fails_without_another_attempt():
    user_id = await _user()
    await _isolate([user_id])
    job_id = await insert_ready(user_id)
    async with worker_session_scope() as session:
        claimed = await claim_one(session, worker_id="w")
    async with worker_session_scope() as session:
        status = await fail_job(
            session,
            job_id=claimed["id"],
            token=claimed["lease_token"],
            task_id=claimed["task_id"],
            error_code="approval_required",
            attempt_count=1,
            max_attempts=3,
        )
    assert status == "failed"
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        row = (
            await session.execute(text("SELECT status, attempt_count FROM jobs WHERE id = :id"), {"id": job_id})
        ).one()
        task = (
            await session.execute(text("SELECT status, error FROM tasks WHERE id = :id"), {"id": claimed["task_id"]})
        ).one()
        events = (
            await session.execute(
                text(
                    "SELECT from_status, to_status, reason, actor "
                    "FROM task_events WHERE task_id = :id"
                ),
                {"id": claimed["task_id"]},
            )
        ).all()
    assert row.status == "failed"
    assert row.attempt_count == 1
    assert task.status == "failed"
    assert task.error == "approval_required"
    assert {
        (event.from_status, event.to_status, event.reason, event.actor)
        for event in events
    } == {
        (None, "created", "test", "test"),
        ("created", "understanding", "approval_required", "worker"),
        ("understanding", "planning", "approval_required", "worker"),
        ("planning", "executing", "approval_required", "worker"),
        ("executing", "failed", "approval_required", "worker"),
    }


async def insert_ready(user_id: uuid.UUID) -> uuid.UUID:
    task_id = await _task(user_id)
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        job_id = await insert_job(session, user_id=user_id, task_id=task_id)
        await session.commit()
    return job_id


_EXPECTED_TRANSITIONS = {
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

_TRANSITION_PAIRS = tuple(
    pytest.param(
        current,
        target,
        id=f"{current.value}-to-{target.value}",
    )
    for current in TaskStatus
    for target in TaskStatus
)


@pytest.mark.parametrize(("current", "target"), _TRANSITION_PAIRS)
def test_all_status_pairs_match_transition_matrix(current, target):
    if target in _EXPECTED_TRANSITIONS[current]:
        assert apply_transition(current, target) == target
        return

    with pytest.raises(IllegalTransition) as error:
        apply_transition(current, target)

    assert error.value.current == current
    assert error.value.target == target


def test_record_transition_returns_event_and_updates_task():
    task = Task(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="test",
        instruction="test",
        status=TaskStatus.CREATED.value,
    )

    event = record_transition(task, TaskStatus.UNDERSTANDING, "test-actor", "accepted")

    assert task.status == TaskStatus.UNDERSTANDING.value
    assert event.from_status == TaskStatus.CREATED.value
    assert event.to_status == TaskStatus.UNDERSTANDING.value
    assert event.actor == "test-actor"
    assert event.reason == "accepted"


def test_record_transition_rejects_illegal_move_without_changing_status():
    task = Task(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="test",
        instruction="test",
        status=TaskStatus.CREATED.value,
    )

    with pytest.raises(IllegalTransition):
        record_transition(task, TaskStatus.COMPLETED, "test-actor", "invalid")

    assert task.status == TaskStatus.CREATED.value
    assert task.error == "illegal transition created -> completed"


@pytest.mark.asyncio
async def test_expired_approval_is_persisted_failed_without_running_steps():
    task = Task(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="t",
        instruction="send",
        status=TaskStatus.WAITING_FOR_APPROVAL.value,
        approval_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    step = TaskStep(
        id=uuid.uuid4(),
        task_id=task.id,
        position=0,
        agent_id="research",
        action="compare",
        risk="low",
        status="pending",
        attempt=0,
    )
    persisted_task, events, marked, executed = {}, [], [], []

    async def persist(current, target, actor, reason):
        persisted_task.update(
            status=target.value, error=reason if target == TaskStatus.FAILED else None
        )
        events.append((current.status, target.value, reason, actor))

    async def still_owner():
        return True

    async def mark_step(current, status):
        marked.append(status)

    async def runner(current):
        executed.append(current.id)
        return "unexpected"

    with pytest.raises(RuntimeError, match="approval_required"):
        await execute_task_durable(
            task,
            [step],
            persist=persist,
            still_owner=still_owner,
            mark_step=mark_step,
            runner=runner,
        )

    assert task.status == TaskStatus.FAILED.value
    assert task.error == "approval_expired"
    assert persisted_task == {"status": "failed", "error": "approval_expired"}
    assert events == [("waiting_for_approval", "failed", "approval_expired", "worker")]
    assert marked == executed == []
