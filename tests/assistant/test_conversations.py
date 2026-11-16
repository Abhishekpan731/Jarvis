"""Conversation persistence and honest handling of a missing provider."""

import uuid

import pytest

from app.ai.provider_factory import provider_status


async def _user(client):
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": f"{uuid.uuid4().hex[:8]}@example.com", "password": "correct horse battery"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_conversation_survives_a_fresh_read(client):
    headers = await _user(client)
    created = await client.post("/api/v1/conversations", headers=headers, json={"title": "Monday"})
    assert created.status_code == 201
    conversation_id = created.json()["id"]

    renamed = await client.patch(
        f"/api/v1/conversations/{conversation_id}",
        headers=headers,
        json={"title": "Monday standup"},
    )
    assert renamed.json()["title"] == "Monday standup"

    listing = await client.get("/api/v1/conversations", headers=headers)
    assert any(row["id"] == conversation_id for row in listing.json()["conversations"])

    removed = await client.delete(f"/api/v1/conversations/{conversation_id}", headers=headers)
    assert removed.status_code == 200
    gone = await client.get(f"/api/v1/conversations/{conversation_id}", headers=headers)
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_message_requires_a_configured_provider(client):
    headers = await _user(client)
    created = await client.post("/api/v1/conversations", headers=headers, json={})
    conversation_id = created.json()["id"]

    reply = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=headers,
        json={"content": "What is on my calendar?"},
    )
    assert provider_status()["configured"] is False
    assert reply.status_code == 503
    assert reply.json()["error"]["code"] == "ai_not_configured"

    # Both turns are stored: the question, and an honest record of the failure.
    history = await client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers)
    assert [row["role"] for row in history.json()["messages"]] == ["user", "assistant"]
    assert "ai_not_configured" in history.json()["messages"][1]["content"]


@pytest.mark.asyncio
async def test_google_assistant_source_hands_the_message_back_instead_of_503(client):
    headers = await _user(client)
    created = await client.post("/api/v1/conversations", headers=headers, json={})
    conversation_id = created.json()["id"]

    reply = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=headers,
        json={"content": "What is on my calendar?", "source": "google_assistant"},
    )
    assert provider_status()["configured"] is False
    assert reply.status_code == 201, reply.text
    body = reply.json()
    assert body["handoff"] is True
    assert body["device_assistant"] == "google_assistant"
    assert body["task_id"] is None
    assert "Google Assistant" in body["content"]

    history = await client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers)
    assert [row["role"] for row in history.json()["messages"]] == ["user", "assistant"]
    assert "Google Assistant" in history.json()["messages"][1]["content"]


@pytest.mark.asyncio
async def test_unknown_message_source_is_rejected(client):
    headers = await _user(client)
    created = await client.post("/api/v1/conversations", headers=headers, json={})
    reply = await client.post(
        f"/api/v1/conversations/{created.json()['id']}/messages",
        headers=headers,
        json={"content": "What is on my calendar?", "source": "telegram"},
    )
    assert reply.status_code == 422
    assert reply.json()["error"]["code"] == "invalid_source"



@pytest.mark.asyncio
async def test_unauthenticated_requests_are_rejected(client):
    listing = await client.get("/api/v1/conversations")
    assert listing.status_code == 401
    health = await client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["ai"]["configured"] is False
