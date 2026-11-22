"""Worker recovery tests distinguish lease loss from an actual process crash."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from app.db import session_scope, set_tenant_guc, worker_session_scope
from app.jobs import claim_one, insert_job, promote_retry_wait, reap_expired
from app.models import Task, TaskEvent, TaskStep, User
from jarvis_worker.loop import Runner
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


@pytest.mark.asyncio
async def test_lease_loss_simulation_then_second_worker_completes(monkeypatch):
    """Expire a lease to simulate worker loss; no worker process is killed."""
    monkeypatch.setattr("app.integrations.runner.google.configured", lambda: True)
    monkeypatch.setattr("app.jobs.backoff_seconds", lambda attempt: 0)

    async def _read(provider, access_token):
        return "1 unread"

    async def _token():
        return {"access_token": "test-token"}

    monkeypatch.setattr("app.integrations.runner.google.read_summary", _read)
    monkeypatch.setattr("app.integrations.runner._load", lambda user_id, provider: _token())
    user_id, task_id = uuid.uuid4(), uuid.uuid4()
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        session.add(User(id=user_id, email=f"{user_id}@example.com", password_hash="x", display_name="t"))
        await session.commit()
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        session.add(Task(id=task_id, user_id=user_id, title="t", instruction="t", status="created"))
        session.add(TaskEvent(task_id=task_id, from_status=None, to_status="created", reason="t", actor="t"))
        session.add(
            TaskStep(
                id=uuid.uuid4(),
                task_id=task_id,
                position=0,
                agent_id="email",
                action="compare",
                risk="low",
                status="pending",
                attempt=0,
            )
        )
        await session.flush()
        job_id = await insert_job(session, user_id=user_id, task_id=task_id)
        await session.commit()
    async with worker_session_scope() as session:
        await session.execute(
            text(
                "UPDATE jobs SET available_at = now() + interval '1 day' "
                "WHERE status IN ('queued', 'retry_wait') AND id <> :id"
            ),
            {"id": job_id},
        )
        await session.commit()

    async with worker_session_scope() as session:
        await set_tenant_guc(session, user_id)
        claimed = await claim_one(session, worker_id="dead", lease_seconds=60)
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        await session.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": claimed["id"]},
        )
        await session.commit()
    async with worker_session_scope() as session:
        await set_tenant_guc(session, user_id)
        assert await reap_expired(session, only_id=claimed["id"]) == 1
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        retry_status = (
            await session.execute(
                text("SELECT status FROM jobs WHERE id = :id"), {"id": claimed["id"]}
            )
        ).scalar_one()
    assert retry_status == "retry_wait"

    async with worker_session_scope() as session:
        assert await promote_retry_wait(session, limit=1) == 1

    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        retry_status = (
            await session.execute(
                text("SELECT status FROM jobs WHERE id = :id"), {"id": claimed["id"]}
            )
        ).scalar_one()
    assert retry_status == "queued"

    async with worker_session_scope() as session:
        await set_tenant_guc(session, user_id)
        again = await claim_one(session, worker_id="live")
    assert again["id"] == claimed["id"]
    assert again["lease_token"] != claimed["lease_token"]
    await Runner("live")._run_job(again)
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        job = (await session.execute(text("SELECT status FROM jobs WHERE id = :id"), {"id": job_id})).scalar()
        task = (await session.execute(text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id})).scalar()
        events = (
            await session.execute(
                text(
                    "SELECT from_status, to_status FROM task_events "
                    "WHERE task_id = :id ORDER BY created_at"
                ),
                {"id": task_id},
            )
        ).all()
    assert job == "completed"
    assert task == "completed"
    assert [(event.from_status, event.to_status) for event in events] == [
        (None, "created"),
        ("created", "understanding"),
        ("understanding", "planning"),
        ("planning", "executing"),
        ("executing", "verifying"),
        ("verifying", "completed"),
    ]


@pytest.mark.asyncio
async def test_email_step_without_a_connection_fails_permanently(monkeypatch):
    from app.integrations import google

    monkeypatch.setattr(google, "configured", lambda: True)
    user_id, task_id = uuid.uuid4(), uuid.uuid4()
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        session.add(User(id=user_id, email=f"{user_id}@example.com", password_hash="x", display_name="t"))
        await session.commit()
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
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
        await session.commit()
    async with worker_session_scope() as session:
        await session.execute(
            text(
                "UPDATE jobs SET available_at = now() + interval '1 day' "
                "WHERE status = 'queued' AND id <> :id"
            ),
            {"id": job_id},
        )
        await session.commit()

    async with worker_session_scope() as session:
        await set_tenant_guc(session, user_id)
        claimed = await claim_one(session, worker_id="live")
    await Runner("live")._run_job(claimed)
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
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
        await set_tenant_guc(session, user_id)
        session.add(User(id=user_id, email=f"{user_id}@example.com", password_hash="x", display_name="t"))
        await session.commit()
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        session.add(Task(id=task_id, user_id=user_id, title="t", instruction="t", status="executing"))
        session.add(TaskEvent(task_id=task_id, from_status="planning", to_status="executing", reason="t", actor="t"))
        await session.flush()
        job_id = await insert_job(session, user_id=user_id, task_id=task_id)
        await session.commit()
    async with worker_session_scope() as session:
        await session.execute(
            text(
                "UPDATE jobs SET available_at = now() + interval '1 day' "
                "WHERE status = 'queued' AND id <> :id"
            ),
            {"id": job_id},
        )
        await session.commit()

    async with worker_session_scope() as session:
        await set_tenant_guc(session, user_id)
        claimed = await claim_one(session, worker_id="live", lease_seconds=60)
    assert claimed["id"] == job_id
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        await session.execute(
            text("UPDATE jobs SET cancel_requested = true WHERE id = :id AND status = 'running'"),
            {"id": job_id},
        )
        await session.commit()

    runner = Runner("live")
    await runner._cancel_if_requested(job_id, uuid.uuid4())
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
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
        await set_tenant_guc(session, user_id)
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


@pytest.mark.parametrize(
    "failure",
    [SQLAlchemyError("database unavailable"), RuntimeError("worker database is misconfigured")],
)
@pytest.mark.asyncio
async def test_worker_health_reports_database_unavailable(monkeypatch, failure):
    import jarvis_worker.health as worker_health

    handlers = []

    class FailedSession:
        async def __aenter__(self):
            raise failure

        async def __aexit__(self, *args):
            return False

    async def fake_start_server(handler, host, port):
        handlers.append(handler)

    class Reader:
        async def readline(self):
            return b"GET /ready HTTP/1.1\r\n"

    class Writer:
        response = b""

        def write(self, response):
            self.response = response

        async def drain(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(asyncio, "start_server", fake_start_server)
    monkeypatch.setattr(worker_health, "worker_session_scope", FailedSession)
    runner = SimpleNamespace(ready=True, shutdown=asyncio.Event(), inflight=0)
    await worker_health.start_health(runner)
    writer = Writer()
    await handlers[0](Reader(), writer)

    assert writer.response.startswith(b"HTTP/1.1 503")
    assert b'"database": "error"' in writer.response

@pytest.mark.asyncio
async def test_guard_logs_and_isolates_unhandled_job_failure(monkeypatch):
    runner = Runner("test-worker")
    runner.inflight = 1

    async def fail_job(job):
        raise ValueError("unexpected task failure")

    monkeypatch.setattr(runner, "_run_job", fail_job)
    await runner._guard({"id": "job-id"})

    assert runner.inflight == 0

@pytest.mark.asyncio
async def test_run_job_persists_unexpected_failure_as_provider_unavailable(monkeypatch):
    runner = Runner("test-worker")
    runner.shutdown.set()
    persisted_failures = []

    async def load_task(task_id, user_id):
        return object(), []

    async def persist_failure(job, error_code):
        persisted_failures.append(error_code)

    async def fail_execution(*args, **kwargs):
        raise ValueError("unexpected task failure")

    monkeypatch.setattr(runner, "_load", load_task)
    monkeypatch.setattr(runner, "_fail", persist_failure)
    monkeypatch.setattr("jarvis_worker.loop.execute_task_durable", fail_execution)
    await runner._run_job(
        {
            "id": "job-id",
            "lease_token": "lease-token",
            "task_id": "task-id",
            "user_id": "user-id",
        }
    )

    assert persisted_failures == ["provider_unavailable"]
