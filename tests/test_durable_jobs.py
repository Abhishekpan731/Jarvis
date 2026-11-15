"""Durable queue behavior. These tests use the real database and the claim SQL."""

import asyncio
import uuid

import pytest
from app.db import session_scope, set_tenant_guc, set_worker_guc
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
    async with session_scope() as session:
        await set_worker_guc(session)
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
    async with session_scope() as left, session_scope() as right:
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
    async with session_scope() as session:
        first = await claim_one(session, worker_id="w1", per_user_max=1)
    async with session_scope() as session:
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
    async with session_scope() as session:
        claimed = await claim_one(session, worker_id="owner")
    assert claimed is not None
    stale = uuid.uuid4()
    async with session_scope() as session:
        alive = await heartbeat(session, claimed["id"], stale)
    assert alive is False
    with pytest.raises(LeaseLost):
        async with session_scope() as session:
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
    async with session_scope() as session:
        claimed = await claim_one(session, worker_id="dead", lease_seconds=60)
    assert claimed is not None
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        await session.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": job_id},
        )
        await session.commit()
    async with session_scope() as session:
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
    async with session_scope() as session:
        claimed = await claim_one(session, worker_id="w")
    async with session_scope() as session:
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
    assert row.status == "failed"
    assert row.attempt_count == 1
    assert task.status == "failed"
    assert task.error == "approval_required"


async def insert_ready(user_id: uuid.UUID) -> uuid.UUID:
    task_id = await _task(user_id)
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        job_id = await insert_job(session, user_id=user_id, task_id=task_id)
        await session.commit()
    return job_id
