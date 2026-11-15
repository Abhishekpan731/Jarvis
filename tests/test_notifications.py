"""In-app notifications. A briefing is a row, not an email, and it is written once."""

import uuid
from datetime import UTC, datetime

import pytest
from app.briefing import due, run_due
from app.db import session_scope, set_worker_guc


async def _register(client, name: str) -> dict:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{name}-{uuid.uuid4()}@example.com",
            "password": "correct-horse",
            "display_name": name,
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_due_uses_the_local_day():
    morning = datetime(2026, 11, 15, 7, 30, tzinfo=UTC)
    assert due(8, 0, "UTC", morning) is None
    assert due(8, 0, "UTC", datetime(2026, 11, 15, 8, 0, tzinfo=UTC)) == "2026-11-15"
    # 08:00 in Kolkata is 02:30 UTC.
    assert due(8, 0, "Asia/Kolkata", datetime(2026, 11, 15, 2, 29, tzinfo=UTC)) is None
    assert due(8, 0, "Asia/Kolkata", datetime(2026, 11, 15, 2, 30, tzinfo=UTC)) == "2026-11-15"


@pytest.mark.asyncio
async def test_schedule_is_private_and_rejects_a_bad_timezone(client):
    owner = await _register(client, "owner")
    other = await _register(client, "other")
    created = await client.get("/api/v1/notifications/schedule", headers=owner)
    assert created.status_code == 200, created.text
    assert created.json()["email_configured"] is False

    bad = await client.put(
        "/api/v1/notifications/schedule",
        headers=owner,
        json={"hour": 9, "minute": 15, "timezone": "Not/AZone", "enabled": True},
    )
    assert bad.status_code == 422

    saved = await client.put(
        "/api/v1/notifications/schedule",
        headers=owner,
        json={"hour": 9, "minute": 15, "timezone": "Asia/Kolkata", "enabled": True},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["hour"] == 9

    foreign = await client.get("/api/v1/notifications/schedule", headers=other)
    assert foreign.json()["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_due_briefing_is_written_once_and_not_visible_to_another_user(client):
    owner = await _register(client, "brief")
    other = await _register(client, "stranger")
    await client.put(
        "/api/v1/notifications/schedule",
        headers=owner,
        json={"hour": 0, "minute": 0, "timezone": "UTC", "enabled": True},
    )
    now = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    async with session_scope() as session:
        await set_worker_guc(session)
        first = await run_due(session, now)
        await session.commit()
    async with session_scope() as session:
        await set_worker_guc(session)
        second = await run_due(session, now)
        await session.commit()
    assert first >= 1
    assert second == 0

    listed = await client.get("/api/v1/notifications", headers=owner)
    assert listed.status_code == 200, listed.text
    notes = listed.json()["notifications"]
    assert len(notes) == 1
    assert notes[0]["kind"] == "briefing"
    assert listed.json()["unread"] == 1
    assert "open tasks" in notes[0]["body"].lower()
    assert "email" in notes[0]["body"].lower()

    hidden = await client.get("/api/v1/notifications", headers=other)
    assert hidden.json()["notifications"] == []

    marked = await client.post(f"/api/v1/notifications/{notes[0]['id']}/read", headers=other)
    assert marked.status_code == 404
    own = await client.post(f"/api/v1/notifications/{notes[0]['id']}/read", headers=owner)
    assert own.status_code == 200
    again = await client.get("/api/v1/notifications", headers=owner)
    assert again.json()["unread"] == 0

    assert again.json()["notifications"][0]["read"] is True


@pytest.mark.asyncio
async def test_a_reminder_is_private_and_fires_once(client):
    owner = await _register(client, "remind")
    other = await _register(client, "outsider")
    created = await client.post(
        "/api/v1/notifications/reminders",
        headers=owner,
        json={"name": "Stand up", "body": "Join the standup", "hour": 0, "minute": 0, "timezone": "UTC"},
    )
    assert created.status_code == 201, created.text
    reserved = await client.post(
        "/api/v1/notifications/reminders",
        headers=owner,
        json={"name": "daily_briefing", "hour": 1, "minute": 0, "timezone": "UTC"},
    )
    assert reserved.status_code == 422

    now = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    async with session_scope() as session:
        await set_worker_guc(session)
        await run_due(session, now)
        await session.commit()
    async with session_scope() as session:
        await set_worker_guc(session)
        await run_due(session, now)
        await session.commit()

    listed = await client.get("/api/v1/notifications", headers=owner)
    reminders = [note for note in listed.json()["notifications"] if note["kind"] == "automation"]
    assert len(reminders) == 1
    assert reminders[0]["body"] == "Join the standup"
    foreign = await client.get("/api/v1/notifications/reminders", headers=other)
    assert foreign.json()["reminders"] == []
    removed = await client.delete(
        f"/api/v1/notifications/reminders/{created.json()['id']}", headers=other
    )
    assert removed.status_code == 404
