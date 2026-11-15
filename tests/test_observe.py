"""The operator view returns counts. It does not return user content."""

import uuid

import pytest

from app.ai.schemas import Usage
from app.config import settings
from app.db import session_scope
from app.models import Conversation, Message, User
from app.observe import queue_depth, record_usage, usage_totals
from app.security import hash_password


async def _register(client):
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": f"{uuid.uuid4().hex[:8]}@example.com", "password": "correct horse battery"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.asyncio
async def test_admin_view_is_closed_without_the_operator_token(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "")
    headers = await _register(client)
    denied = await client.get("/api/v1/admin/overview", headers=headers)
    assert denied.status_code == 401
    blank = await client.get("/api/v1/admin/overview", headers={"Authorization": "Bearer "})
    assert blank.status_code == 401


@pytest.mark.asyncio
async def test_admin_view_returns_counts_and_no_message_text(client, monkeypatch):
    token = "operator-token-for-test"
    monkeypatch.setattr(settings, "admin_token", token)
    headers = await _register(client)
    secret = "the user wrote this and an operator must not see it"
    async with session_scope() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("correct horse battery"),
            display_name="Test",
        )
        session.add(user)
        await session.flush()
        conversation = Conversation(id=uuid.uuid4(), user_id=user.id, title="private")
        session.add(conversation)
        await session.flush()
        session.add(
            Message(
                id=uuid.uuid4(),
                conversation_id=conversation.id,
                user_id=user.id,
                role="user",
                content=secret,
                content_type="text",
            )
        )
        await record_usage(
            session,
            user_id=user.id,
            task_id=None,
            provider="openai",
            model="gpt-4o-mini",
            usage=Usage(input_tokens=10, output_tokens=4),
        )
        await session.commit()
        totals = await usage_totals(session)
        depth = await queue_depth(session)

    assert totals["input_tokens"] >= 10
    assert totals["calls"] >= 1
    assert set(depth) == {"queued", "running", "retry_wait", "failed"}

    response = await client.get(
        "/api/v1/admin/overview", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["content"] is None
    assert secret not in response.text
    assert "email" not in body
    assert body["usage"]["calls"] >= 1
    user_view = await client.get("/api/v1/admin/overview", headers=headers)
    assert user_view.status_code == 401
