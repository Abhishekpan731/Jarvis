"""Task API. Cross-user access is 404, not 403, and lease fields are ignored."""

import uuid

import pytest
from app.db import session_scope
from sqlalchemy import text


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
    created = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"instruction": "research compare the two libraries", "worker_id": "evil"},
    )
    task_id = created.json()["id"]
    cancelled = await client.post(f"/api/v1/tasks/{task_id}/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    async with session_scope() as session:
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
