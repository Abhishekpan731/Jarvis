"""A dead worker's job is recovered by the next worker, not lost."""

import uuid

import pytest
from app.db import session_scope
from app.jobs import claim_one, insert_job, reap_expired
from app.models import Task, TaskEvent, TaskStep, User
from jarvis_worker.loop import Runner
from sqlalchemy import text


@pytest.mark.asyncio
async def test_crash_then_second_worker_completes():
    user_id, task_id = uuid.uuid4(), uuid.uuid4()
    async with session_scope() as session:
        session.add(User(id=user_id, email=f"{user_id}@example.com", password_hash="x", display_name="t"))
        await session.commit()
    async with session_scope() as session:
        session.add(Task(id=task_id, user_id=user_id, title="t", instruction="t", status="created"))
        session.add(TaskEvent(task_id=task_id, from_status=None, to_status="created", reason="t", actor="t"))
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
        await session.flush()
        job_id = await insert_job(session, user_id=user_id, task_id=task_id)
        await session.execute(
            text(
                "UPDATE jobs SET available_at = now() + interval '1 day' "
                "WHERE status = 'queued' AND id <> :id"
            ),
            {"id": job_id},
        )
        await session.commit()

    async with session_scope() as session:
        claimed = await claim_one(session, worker_id="dead", lease_seconds=60)
    async with session_scope() as session:
        await session.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": claimed["id"]},
        )
        await session.commit()
    async with session_scope() as session:
        assert await reap_expired(session, only_id=claimed["id"]) == 1
        await session.execute(
            text(
                "UPDATE jobs SET status = 'queued', available_at = now() "
                "WHERE id = :id AND status = 'retry_wait'"
            ),
            {"id": claimed["id"]},
        )
        await session.commit()

    async with session_scope() as session:
        again = await claim_one(session, worker_id="live")
    assert again["id"] == claimed["id"]
    assert again["lease_token"] != claimed["lease_token"]
    await Runner("live")._run_job(again)
    async with session_scope() as session:
        job = (await session.execute(text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id})).scalar()
        task = (await session.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id})).scalar()
    assert job == "completed"
    assert task == "completed"


@pytest.mark.asyncio
async def test_email_step_without_a_connection_fails_permanently(monkeypatch):
    from app.integrations import google

    monkeypatch.setattr(google, "configured", lambda: True)
    user_id, task_id = uuid.uuid4(), uuid.uuid4()
    async with session_scope() as session:
        session.add(User(id=user_id, email=f"{user_id}@example.com", password_hash="x", display_name="t"))
        await session.commit()
    async with session_scope() as session:
        session.add(Task(id=task_id, user_id=user_id, title="t", instruction="email the inbox", status="created"))
        session.add(TaskEvent(task_id=task_id, from_status=None, to_status="created", reason="t", actor="t"))
        session.add(
            TaskStep(
                id=uuid.uuid4(),
                task_id=task_id,
                position=0,
                agent_id="email",
                action="read",
                risk="low",
                status="pending",
                attempt=0,
            )
        )
        await session.flush()
        job_id = await insert_job(session, user_id=user_id, task_id=task_id)
        await session.execute(
            text("UPDATE jobs SET available_at = now() + interval '1 day' WHERE status = 'queued' AND id <> :id"),
            {"id": job_id},
        )
        await session.commit()

    async with session_scope() as session:
        claimed = await claim_one(session, worker_id="live")
    await Runner("live")._run_job(claimed)
    async with session_scope() as session:
        row = (
            await session.execute(
                text("SELECT status, last_error FROM jobs WHERE id = :id"), {"id": job_id}
            )
        ).one()
        task = (await session.execute(text("SELECT status, error FROM tasks WHERE id = :id"), {"id": task_id})).one()
    assert row.status == "failed"
    assert row.last_error == "integration_required"
    assert task.status == "failed"
    assert task.error == "integration_required"



@pytest.mark.asyncio
async def test_running_job_cancels_under_lease_token():
    user_id, task_id = uuid.uuid4(), uuid.uuid4()
    async with session_scope() as session:
        session.add(User(id=user_id, email=f"{user_id}@example.com", password_hash="x", display_name="t"))
        await session.commit()
    async with session_scope() as session:
        session.add(Task(id=task_id, user_id=user_id, title="t", instruction="t", status="executing"))
        session.add(TaskEvent(task_id=task_id, from_status="planning", to_status="executing", reason="t", actor="t"))
        await session.flush()
        job_id = await insert_job(session, user_id=user_id, task_id=task_id)
        await session.execute(
            text("UPDATE jobs SET available_at = now() + interval '1 day' WHERE status = 'queued' AND id <> :id"),
            {"id": job_id},
        )
        await session.commit()

    async with session_scope() as session:
        claimed = await claim_one(session, worker_id="live", lease_seconds=60)
    assert claimed["id"] == job_id
    async with session_scope() as session:
        await session.execute(
            text("UPDATE jobs SET cancel_requested = true WHERE id = :id AND status = 'running'"),
            {"id": job_id},
        )
        await session.commit()

    runner = Runner("live")
    await runner._cancel_if_requested(job_id, uuid.uuid4())
    async with session_scope() as session:
        stale = (
            await session.execute(
                text("SELECT status, lease_token, cancel_requested FROM jobs WHERE id = :id"),
                {"id": job_id},
            )
        ).one()
        task = (await session.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id})).scalar()
    assert stale.status == "running"
    assert stale.cancel_requested is True
    assert stale.lease_token == claimed["lease_token"]
    assert task == "executing"

    await runner._cancel_if_requested(job_id, claimed["lease_token"])
    async with session_scope() as session:
        row = (
            await session.execute(
                text("SELECT status, lease_token, cancel_requested, last_error FROM jobs WHERE id = :id"),
                {"id": job_id},
            )
        ).one()
        task = (await session.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id})).scalar()
    assert row.status == "cancelled"
    assert row.lease_token is None
    assert row.cancel_requested is False
    assert row.last_error == "cancelled"
    assert task == "cancelled"
