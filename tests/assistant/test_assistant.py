"""Command gateway, device registration, and cross-device sync.

These exercise the backend contract only. They do not claim that Siri or Google
Assistant were invoked on a physical device.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import app.routers.assistant as assistant_router
import pytest
from app.ai.schemas import Plan
from fastapi import HTTPException


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
    assert [event["kind"] for event in events] == [
        "conversation.created",
        "conversation.updated",
        "message.created",
        "message.created",
        "command.accepted",
    ]
    assert events[-1]["payload"]["task_id"] == body["task_id"]
    assert events[-1]["device_id"] == device_id
    assert events[2]["payload"]["message"]["content"] == "research compare the two libraries"

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
    synced = await client.get("/api/v1/sync", headers=headers)
    assert [event["kind"] for event in synced.json()["events"]] == [
        "conversation.created",
        "conversation.updated",
        "message.created",
        "message.created",
        "command.accepted",
    ]

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



class _CommandSession:
    def __init__(self, conversation, replay_store):
        self.conversation = conversation
        self.replay_store = replay_store
        self.added = []
        self.commits = 0

    async def execute(self, _statement, parameters):
        result = self.replay_store.get((parameters["user_id"], parameters["key"]))
        row = {"payload": {"result": result}} if result is not None else None
        mapping = SimpleNamespace(one_or_none=lambda: row)
        return SimpleNamespace(mappings=lambda: mapping)

    async def get(self, model, row_id):
        if model is assistant_router.Conversation and row_id == self.conversation.id:
            return self.conversation
        return None

    def add(self, item):
        self.added.append(item)
        if isinstance(item, assistant_router.SyncEvent) and item.kind == "command.idempotency":
            self.replay_store[(item.user_id, item.payload["key"])] = item.payload["result"]

    async def flush(self):
        for item in self.added:
            if isinstance(item, assistant_router.Message) and item.created_at is None:
                item.created_at = datetime.now(UTC)

    async def commit(self):
        self.commits += 1


class _AccountLimiter:
    def __init__(self):
        self.counts = {}
        self.calls = []

    async def limit(self, user_id):
        self.calls.append(user_id)
        if self.counts.get(user_id, 0) >= 1:
            raise HTTPException(429, "limited")
        self.counts[user_id] = self.counts.get(user_id, 0) + 1


def _configure_command_stubs(monkeypatch, limiter):
    async def plan(_model, _command):
        return Plan(
            intent="conversation", requires_execution=False, agents=[], risk="low",
            requires_approval=False, steps=[], direct_answer="Acknowledged.",
        ), None

    monkeypatch.setattr(assistant_router, "limit_writes_durable", limiter.limit)
    monkeypatch.setattr(
        assistant_router, "get_model", lambda: SimpleNamespace(name="test", model="test-model")
    )
    monkeypatch.setattr(assistant_router, "plan_request", plan)


def _command_session(user_id, replay_store):
    conversation = SimpleNamespace(
        id=uuid.uuid4(), user_id=user_id, deleted_at=None, title="Assistant",
        status="active", created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    return _CommandSession(conversation, replay_store)


@pytest.mark.asyncio
async def test_command_replay_does_not_consume_another_limit_slot(monkeypatch):
    limiter = _AccountLimiter()
    _configure_command_stubs(monkeypatch, limiter)
    owner_id = uuid.uuid4()
    session = _command_session(owner_id, {})
    body = assistant_router.CommandIn(
        command="say hello", conversation_id=session.conversation.id
    )

    first = await assistant_router.submit_command(
        body, SimpleNamespace(id=owner_id), session, "retry-key"
    )
    write_count = len(session.added)
    replay = await assistant_router.submit_command(
        body, SimpleNamespace(id=owner_id), session, "retry-key"
    )

    assert replay == first
    assert len(session.added) == write_count
    assert session.commits == 1
    assert limiter.calls == [owner_id]


@pytest.mark.asyncio
async def test_command_limit_is_owner_scoped_and_returns_429(monkeypatch):
    limiter = _AccountLimiter()
    _configure_command_stubs(monkeypatch, limiter)
    owner_id, other_id = uuid.uuid4(), uuid.uuid4()
    replay_store = {}
    owner_session = _command_session(owner_id, replay_store)
    other_session = _command_session(other_id, replay_store)

    first = await assistant_router.submit_command(
        assistant_router.CommandIn(
            command="say hello", conversation_id=owner_session.conversation.id
        ), SimpleNamespace(id=owner_id), owner_session, "shared-key"
    )
    other = await assistant_router.submit_command(
        assistant_router.CommandIn(
            command="say hello", conversation_id=other_session.conversation.id
        ), SimpleNamespace(id=other_id), other_session, "shared-key"
    )

    write_count = len(owner_session.added)
    with pytest.raises(HTTPException) as caught:
        await assistant_router.submit_command(
            assistant_router.CommandIn(command="new command"),
            SimpleNamespace(id=owner_id), owner_session, "new-key"
        )
    assert len(owner_session.added) == write_count
    assert owner_session.commits == 1
    assert other["conversation_id"] != first["conversation_id"]
    assert caught.value.status_code == 429
    assert limiter.calls == [owner_id, other_id, owner_id]
