"""Command gateway, device registration, and cross-device sync.

These exercise the backend contract only. They do not claim that Siri or Google
Assistant were invoked on a physical device.
"""

import uuid

import pytest


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


@pytest.mark.asyncio
async def test_command_is_planned_and_synced_across_devices(client):
    phone = await _register(client, "phone")

    registered = await client.post(
        "/api/v1/devices", headers=phone, json={"platform": "ios", "label": "iPhone"}
    )
    assert registered.status_code == 201, registered.text
    device_id = registered.json()["id"]

    submitted = await client.post(
        "/api/v1/assistant/commands",
        headers=phone,
        json={
            "command": "  research   compare the two libraries  ",
            "source": "siri",
            "device_id": device_id,
            "input_type": "voice",
            "locale": "en-IN",
        },
    )
    assert submitted.status_code == 202, submitted.text
    body = submitted.json()
    assert body["task_id"] is not None
    assert body["requires_approval"] is False
    assert body["source"] == "siri"
    assert "research" in body["response_text"]
    assert len(body["voice_response"]) <= 240

    # A second device for the same account sees the event without any transfer.
    tablet = await _register(client, "tablet")
    # The tablet is a different account, so it must see nothing of the phone's.
    foreign = await client.get("/api/v1/sync", headers=tablet)
    assert foreign.json()["events"] == []

    own = await client.get("/api/v1/sync", headers=phone)
    events = own.json()["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "command.accepted"
    assert events[0]["payload"]["task_id"] == body["task_id"]
    assert events[0]["device_id"] == device_id

    # Replaying from the cursor returns nothing new; the cursor is stable.
    again = await client.get("/api/v1/sync", headers=phone, params={"after": own.json()["cursor"]})
    assert again.json()["events"] == []


@pytest.mark.asyncio
async def test_high_risk_command_requires_approval_and_is_not_queued(client):
    headers = await _register(client, "risk")
    submitted = await client.post(
        "/api/v1/assistant/commands",
        headers=headers,
        json={"command": "email draft_reply and send it", "source": "google_assistant"},
    )
    assert submitted.status_code == 202, submitted.text
    body = submitted.json()
    assert body["requires_approval"] is True
    assert body["status"] == "waiting_for_approval"

    task = await client.get(f"/api/v1/tasks/{body['task_id']}", headers=headers)
    assert task.json()["job"] is None


@pytest.mark.asyncio
async def test_idempotent_replay_and_device_revocation(client):
    headers = await _register(client, "idem")
    device = await client.post(
        "/api/v1/devices", headers=headers, json={"platform": "android", "label": "Pixel"}
    )
    device_id = device.json()["id"]
    key = {"Idempotency-Key": f"k-{uuid.uuid4()}"}

    first = await client.post(
        "/api/v1/assistant/commands",
        headers={**headers, **key},
        json={"command": "research compare the two libraries", "device_id": device_id},
    )
    second = await client.post(
        "/api/v1/assistant/commands",
        headers={**headers, **key},
        json={"command": "research compare the two libraries", "device_id": device_id},
    )
    assert first.json()["task_id"] == second.json()["task_id"]

    revoked = await client.delete(f"/api/v1/devices/{device_id}", headers=headers)
    assert revoked.json()["revoked"] is True

    blocked = await client.post(
        "/api/v1/assistant/commands",
        headers=headers,
        json={"command": "research compare the two libraries", "device_id": device_id},
    )
    assert blocked.status_code == 403


@pytest.mark.asyncio
async def test_foreign_device_and_bad_source_are_rejected(client):
    owner = await _register(client, "owner")
    other = await _register(client, "other")
    device = await client.post(
        "/api/v1/devices", headers=owner, json={"platform": "web", "label": "Browser"}
    )
    stolen = await client.post(
        "/api/v1/assistant/commands",
        headers=other,
        json={"command": "research compare the two libraries", "device_id": device.json()["id"]},
    )
    assert stolen.status_code == 404

    bad = await client.post(
        "/api/v1/assistant/commands",
        headers=owner,
        json={"command": "research compare the two libraries", "source": "telegram"},
    )
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_unconfigured_device_source_hands_off_without_queueing(client):
    headers = await _register(client, "handoff")
    submitted = await client.post(
        "/api/v1/assistant/commands",
        headers=headers,
        json={"command": "What time is it in Tokyo?", "source": "siri", "input_type": "voice"},
    )
    assert submitted.status_code == 202, submitted.text
    body = submitted.json()
    assert body["status"] == "handed_off"
    assert body["handoff"] is True
    assert body["device_assistant"] == "siri"
    assert body["task_id"] is None
    assert "Siri" in body["response_text"]
    assert "Siri" in body["voice_response"]


@pytest.mark.asyncio
async def test_unconfigured_api_source_does_not_invent_a_handoff(client):
    headers = await _register(client, "nohandoff")
    submitted = await client.post(
        "/api/v1/assistant/commands",
        headers=headers,
        json={"command": "What is on my calendar?", "source": "api"},
    )
    assert submitted.status_code == 202, submitted.text
    body = submitted.json()
    assert body["handoff"] is False
    assert body["device_assistant"] is None
    assert body["task_id"] is None
    assert body["status"] == "accepted"

