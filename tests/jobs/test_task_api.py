"""Task API. Cross-user access is 404, not 403, and lease fields are ignored."""

import uuid

import pytest
from app.ai.schemas import Plan, PlanStep
from app.db import session_scope, set_tenant_guc
from app.jobs import IdempotencyConflict, request_hash, resolve_idempotency
from app.tasks_service import create_task_from_plan
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


async def _register(client, name: str) -> dict:
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": f"{name}-{uuid.uuid4()}@example.com", "password": "correct-horse", "display_name": name},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_create_lists_and_hides_other_users(client):
    owner = await _register(client, "owner")
    other = await _register(client, "other")
    created = await client.post(
        "/api/v1/tasks",
        headers=owner,
        json={"instruction": "research compare the two libraries"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["job"]["status"] == "queued"
    assert "lease_token" not in body["job"]
    task_id = body["id"]

    missing = await client.get(f"/api/v1/tasks/{task_id}", headers=other)
    assert missing.status_code == 404
    jobs = await client.get(f"/api/v1/tasks/{task_id}/jobs", headers=other)
    assert jobs.status_code == 404

    listed = await client.get("/api/v1/tasks", headers=owner)
    assert task_id in {item["id"] for item in listed.json()["tasks"]}
    other_list = await client.get("/api/v1/tasks", headers=other)
    assert task_id not in {item["id"] for item in other_list.json()["tasks"]}


@pytest.mark.asyncio
async def test_idempotency_key_replays_and_conflicts(client):
    headers = await _register(client, "idem")
    key = {"Idempotency-Key": f"k-{uuid.uuid4()}"}
    first = await client.post(
        "/api/v1/tasks",
        headers={**headers, **key},
        json={"instruction": "research compare the two libraries"},
    )
    second = await client.post(
        "/api/v1/tasks",
        headers={**headers, **key},
        json={"instruction": "research compare the two libraries"},
    )
    assert first.json()["id"] == second.json()["id"]
    conflict = await client.post(
        "/api/v1/tasks",
        headers={**headers, **key},
        json={"instruction": "research something else entirely"},
    )
    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_cancel_queued_job_and_reject_lease_fields(client):
    headers = await _register(client, "cancel")
    rejected = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"instruction": "research compare the two libraries", "worker_id": "evil"},
    )
    assert rejected.status_code == 400
    created = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"instruction": "research compare the two libraries"},
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["id"]
    cancelled = await client.post(f"/api/v1/tasks/{task_id}/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    user_id = await _user_id(client, headers)
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        status = (
            await session.execute(
                text("SELECT status FROM jobs WHERE task_id = :id"),
                {"id": uuid.UUID(task_id)},
            )
        ).scalar_one()
    assert status == "cancelled"


@pytest.mark.asyncio
async def test_approval_plan_is_not_enqueued(client):
    headers = await _register(client, "gate")
    created = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"instruction": "research compare the libraries and send the result"},
    )
    body = created.json()
    assert body["status"] == "waiting_for_approval"
    assert body["job"] is None


async def _counts(user_id: uuid.UUID) -> tuple[int, int, int]:
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        tasks = (
            await session.execute(
                text("SELECT count(*) FROM tasks WHERE user_id = :id"), {"id": user_id}
            )
        ).scalar_one()
        jobs = (
            await session.execute(
                text("SELECT count(*) FROM jobs WHERE user_id = :id"), {"id": user_id}
            )
        ).scalar_one()
        keys = (
            await session.execute(
                text("SELECT count(*) FROM idempotency_keys WHERE user_id = :id"),
                {"id": user_id},
            )
        ).scalar_one()
    return int(tasks), int(jobs), int(keys)


async def _user_id(client, headers: dict) -> uuid.UUID:
    me = await client.get("/api/v1/auth/session", headers=headers)
    assert me.status_code == 200, me.text
    return uuid.UUID(me.json()["user"]["id"])


@pytest.mark.asyncio
async def test_idempotency_replay_returns_the_same_task(client):
    headers = await _register(client, "replay")
    user_id = await _user_id(client, headers)
    key = {"Idempotency-Key": f"replay-{uuid.uuid4()}"}
    body = {"instruction": "research compare the two libraries", "priority": 3}
    first = await client.post("/api/v1/tasks", headers={**headers, **key}, json=body)
    second = await client.post("/api/v1/tasks", headers={**headers, **key}, json=body)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] == second.json()["id"]
    assert (await _counts(user_id)) == (1, 1, 1)
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        priority = (
            await session.execute(
                text("SELECT priority FROM jobs WHERE user_id = :id"), {"id": user_id}
            )
        ).scalar_one()
    assert priority == 3


@pytest.mark.asyncio
async def test_same_key_different_hash_is_409_and_creates_nothing(client):
    headers = await _register(client, "clash")
    user_id = await _user_id(client, headers)
    key = {"Idempotency-Key": f"clash-{uuid.uuid4()}"}
    first = await client.post(
        "/api/v1/tasks",
        headers={**headers, **key},
        json={"instruction": "research compare the two libraries"},
    )
    assert first.status_code == 200, first.text
    conflict = await client.post(
        "/api/v1/tasks",
        headers={**headers, **key},
        json={"instruction": "research something else entirely"},
    )
    assert conflict.status_code == 409
    assert (await _counts(user_id)) == (1, 1, 1)
    listed = await client.get("/api/v1/tasks", headers=headers)
    assert [item["id"] for item in listed.json()["tasks"]] == [first.json()["id"]]


@pytest.mark.asyncio
async def test_same_key_different_user_creates_two_tasks(client):
    first_headers = await _register(client, "alice")
    second_headers = await _register(client, "bob")
    key = f"shared-{uuid.uuid4()}"
    body = {"instruction": "research compare the two libraries"}
    first = await client.post(
        "/api/v1/tasks",
        headers={**first_headers, "Idempotency-Key": key},
        json=body,
    )
    second = await client.post(
        "/api/v1/tasks",
        headers={**second_headers, "Idempotency-Key": key},
        json=body,
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] != second.json()["id"]
    alice = await _user_id(client, first_headers)
    bob = await _user_id(client, second_headers)
    assert (await _counts(alice)) == (1, 1, 1)
    assert (await _counts(bob)) == (1, 1, 1)


@pytest.mark.asyncio
async def test_unique_violation_rolls_back_task_job_and_key(client):
    """A losing create of the same key leaves no extra task, job, or key."""
    headers = await _register(client, "race")
    user_id = await _user_id(client, headers)
    key = f"race-{uuid.uuid4()}"
    body = {"instruction": "research compare the two libraries", "priority": 2}
    created = await client.post(
        "/api/v1/tasks",
        headers={**headers, "Idempotency-Key": key},
        json=body,
    )
    assert created.status_code == 200, created.text
    original = uuid.UUID(created.json()["id"])
    digest = request_hash(body["instruction"], body["priority"])
    plan = Plan(
        intent="compare the two libraries",
        requires_execution=True,
        agents=["research"],
        risk="low",
        requires_approval=False,
        steps=[PlanStep(agent="research", action="compare the two libraries")],
    )

    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        try:
            await create_task_from_plan(
                session,
                user_id,
                None,
                plan,
                enqueue=True,
                commit=False,
                priority=2,
                idempotency_key=key,
                request_hash=digest,
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
        else:
            raise AssertionError("duplicate key insert must violate the unique constraint")

    assert (await _counts(user_id)) == (1, 1, 1)
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        replayed = await resolve_idempotency(session, user_id, key, digest)
        with pytest.raises(IdempotencyConflict):
            await resolve_idempotency(session, user_id, key, "different-hash")
        task_ids = (
            await session.execute(
                text("SELECT id FROM tasks WHERE user_id = :id"), {"id": user_id}
            )
        ).scalars().all()
    assert replayed == original
    assert task_ids == [original]


@pytest.mark.asyncio
async def test_approval_key_commits_without_a_job(client):
    headers = await _register(client, "gated-key")
    user_id = await _user_id(client, headers)
    key = {"Idempotency-Key": f"gate-{uuid.uuid4()}"}
    created = await client.post(
        "/api/v1/tasks",
        headers={**headers, **key},
        json={"instruction": "research compare the libraries and send the result"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "waiting_for_approval"
    assert created.json()["job"] is None
    assert (await _counts(user_id)) == (1, 0, 1)
    replay = await client.post(
        "/api/v1/tasks",
        headers={**headers, **key},
        json={"instruction": "research compare the libraries and send the result"},
    )
    assert replay.json()["id"] == created.json()["id"]
    assert (await _counts(user_id)) == (1, 0, 1)


@pytest.mark.asyncio
async def test_client_cannot_set_user_lease_or_status(client):
    headers = await _register(client, "owned")
    for extra in (
        {"user_id": str(uuid.uuid4())},
        {"lease_token": str(uuid.uuid4())},
        {"status": "completed"},
    ):
        rejected = await client.post(
            "/api/v1/tasks",
            headers=headers,
            json={"instruction": "research compare the two libraries", **extra},
        )
        assert rejected.status_code == 400, extra
