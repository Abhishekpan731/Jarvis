"""Conversation persistence and honest handling of a missing provider."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import app.routers.conversations as conversation_router
import jwt
import pytest
from app.ai.base import ProviderError
from app.ai.openai_provider import OpenAIModel
from app.ai.provider_factory import provider_status
from app.ai.schemas import Completion, Plan, Usage
from app.config import settings
from app.db import session_scope, set_tenant_guc
from app.models import Conversation, Memory, Message, User
from app.observe import usage_totals
from app.routers.conversations import MessageIn, _retained_history_count
from app.routers.conversations import stream as stream_route
from fastapi import HTTPException
from sqlalchemy import select


async def _user(client):
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": f"{uuid.uuid4().hex[:8]}@example.com", "password": "correct horse battery"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _user_id(headers: dict) -> UUID:
    token = headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    return UUID(claims["sub"])


async def _usage_totals() -> dict:
    async with session_scope() as session:
        return await usage_totals(session)


async def _message_context(session, conversation) -> list[dict]:
    """Build the message-only context needed by stream persistence tests."""
    rows = (
        await session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.role.in_(("user", "assistant")),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(40)
        )
    ).scalars().all()
    return [{"role": row.role, "content": row.content} for row in reversed(rows)]


def _sse_events(content: str) -> list[tuple[str, dict]]:
    events = []
    for block in content.strip().split("\n\n"):
        if not block:
            continue
        event = next(line[7:] for line in block.splitlines() if line.startswith("event: "))
        data = next(line[6:] for line in block.splitlines() if line.startswith("data: "))
        events.append((event, json.loads(data)))
    return events


class _StreamingModel:
    name = "openai"
    model = "gpt-4o-mini"

    def __init__(self, parts: list[str], usage: Usage, *, fail: bool = False):
        self.parts = parts
        self._usage = usage
        self.fail = fail

    async def stream(self, messages: list[dict]):
        for part in self.parts:
            yield part
        if self.fail:
            raise ProviderError("stub stream failed")

    def usage(self) -> Usage:
        return self._usage
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
    events = (await client.get("/api/v1/sync", headers=headers)).json()["events"]
    assert [event["kind"] for event in events] == [
        "conversation.created", "message.created", "conversation.updated", "message.created"
    ]


@pytest.mark.asyncio
async def test_message_reports_a_provider_failure_as_503_not_500(client, monkeypatch):
    class _FailingModel:
        name = "openai"
        model = "gpt-test"

        async def complete(self, *args, **kwargs):
            raise ProviderError("The provider rejected the request.")

        async def stream(self, *args, **kwargs):
            raise ProviderError("The provider rejected the request.")

    monkeypatch.setattr("app.routers.conversations.get_model", lambda: _FailingModel())

    headers = await _user(client)
    created = await client.post("/api/v1/conversations", headers=headers, json={})
    conversation_id = created.json()["id"]

    reply = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=headers,
        json={"content": "What is on my calendar?"},
    )
    assert reply.status_code == 503
    assert reply.json()["error"]["code"] == "provider_error"

    # Both turns are stored: the question, and an honest record of the failure.
    history = await client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers)
    assert [row["role"] for row in history.json()["messages"]] == ["user", "assistant"]
    assert "provider_error" in history.json()["messages"][1]["content"]


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


async def _create_conversation(client, headers):
    response = await client.post("/api/v1/conversations", headers=headers, json={})
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.mark.asyncio
async def test_conversation_sync_feed_is_owner_scoped_and_replayable(client):
    owner = await _user(client)
    other = await _user(client)
    conversation_id = await _create_conversation(client, owner)
    renamed = await client.patch(
        f"/api/v1/conversations/{conversation_id}", headers=owner, json={"title": "Updated"}
    )
    removed = await client.delete(f"/api/v1/conversations/{conversation_id}", headers=owner)
    assert renamed.status_code == 200
    assert removed.status_code == 200

    first = await client.get("/api/v1/sync", headers=owner, params={"limit": 2})
    second = await client.get(
        "/api/v1/sync", headers=owner, params={"after": first.json()["cursor"], "limit": 2}
    )
    events = first.json()["events"] + second.json()["events"]
    assert [event["kind"] for event in events] == [
        "conversation.created", "conversation.updated", "conversation.deleted"
    ]
    replay = await client.get("/api/v1/sync", headers=owner, params={"limit": 2})
    assert [event["id"] for event in replay.json()["events"]] == [
        event["id"] for event in first.json()["events"]
    ]
    assert (await client.get("/api/v1/sync", headers=other)).json()["events"] == []
    assert (await client.get("/api/v1/sync", headers=owner, params={"after": -1})).status_code == 422


@pytest.mark.asyncio
async def test_stream_persists_reply_and_exact_usage_totals(client, monkeypatch):
    headers = await _user(client)
    conversation_id = await _create_conversation(client, headers)
    model = _StreamingModel(["Hello", " there"], Usage(input_tokens=7, output_tokens=3))
    monkeypatch.setattr("app.routers.conversations.get_model", lambda: model)
    monkeypatch.setattr("app.routers.conversations.build_context", _message_context)
    before = await _usage_totals()

    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/stream",
        headers=headers,
        json={"content": "Say hello"},
    )
    events = _sse_events(response.text)
    assert response.status_code == 200
    assert [event for event, _ in events] == [
        "response.started", "text.delta", "text.delta", "response.completed"
    ]
    assert events[-1][1]["text"] == "Hello there"
    history = await client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers)
    assistant = history.json()["messages"][-1]
    assert assistant["content"] == events[-1][1]["text"]
    assert assistant["token_usage"] == {
        "input_tokens": 7, "output_tokens": 3, "estimated_cost_usd": 0.000003
    }
    after = await _usage_totals()
    assert after["input_tokens"] - before["input_tokens"] == 7
    assert after["output_tokens"] - before["output_tokens"] == 3
    assert after["calls"] - before["calls"] == 1
    assert after["estimated_cost_usd"] - before["estimated_cost_usd"] == pytest.approx(0.000003)


@pytest.mark.asyncio
async def test_stream_error_persists_the_partial_text_the_client_saw(client, monkeypatch):
    headers = await _user(client)
    conversation_id = await _create_conversation(client, headers)
    model = _StreamingModel(["partial reply"], Usage(input_tokens=9, output_tokens=2), fail=True)
    monkeypatch.setattr("app.routers.conversations.get_model", lambda: model)
    monkeypatch.setattr("app.routers.conversations.build_context", _message_context)

    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/stream",
        headers=headers,
        json={"content": "Start an answer"},
    )
    events = _sse_events(response.text)
    assert "text.delta" in [event for event, _ in events]
    assert events[-1] == ("error", {"code": "provider_error", "message": "stub stream failed"})
    assert "response.completed" not in [event for event, _ in events]
    history = await client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers)
    assert history.json()["messages"][-1]["content"] == "partial reply"


@pytest.mark.asyncio
async def test_aborted_stream_persists_the_visible_partial_reply(client, monkeypatch):
    headers = await _user(client)
    user_id = _user_id(headers)
    conversation_id = await _create_conversation(client, headers)

    class _BlockingModel(_StreamingModel):
        async def stream(self, messages: list[dict]):
            yield "visible fragment"
            await asyncio.Event().wait()

    monkeypatch.setattr(
        "app.routers.conversations.get_model",
        lambda: _BlockingModel([], Usage(input_tokens=4, output_tokens=2)),
    )
    monkeypatch.setattr("app.routers.conversations.build_context", _message_context)
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        user = await session.get(User, user_id)
        response = await stream_route(
            UUID(conversation_id), MessageIn(content="Begin then stop"), user, session
        )
        iterator = response.body_iterator
        assert "response.started" in await anext(iterator)
        assert "visible fragment" in await anext(iterator)
        await iterator.aclose()

    history = await client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=headers)
    assert history.json()["messages"][-1]["content"] == "visible fragment"


@pytest.mark.asyncio
async def test_truncated_history_is_summarized_with_bounds_without_memory_rows(
    client, monkeypatch
):
    headers = await _user(client)
    user_id = _user_id(headers)
    conversation_id = await _create_conversation(client, headers)
    memory_secret = "sensitive-memory-content-not-for-summary"
    await _seed_truncated_history(user_id, conversation_id, memory_secret)
    model = _SummaryModel()
    monkeypatch.setattr("app.routers.conversations.get_model", lambda: model)
    monkeypatch.setattr("app.routers.conversations.plan_request", _no_plan)

    reply = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=headers, json={"content": "What did we decide?"},
    )
    assert reply.status_code == 201, reply.text
    _assert_summary_is_bounded_and_memory_free(model, memory_secret)

    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        saved = await session.get(Conversation, UUID(conversation_id))
        summary_text = saved.summary.split("\n", 1)[1]
    assert len(summary_text) == 2_000


@pytest.mark.asyncio
async def test_openai_stream_requests_and_captures_provider_usage():
    kwargs = {}

    async def _chunks():
        yield SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="hello"))], usage=None
        )
        yield SimpleNamespace(
            choices=[], usage=SimpleNamespace(prompt_tokens=8, completion_tokens=2)
        )

    class _Completions:
        async def create(self, **options):
            kwargs.update(options)
            return _chunks()

    model = OpenAIModel.__new__(OpenAIModel)
    model.model = "gpt-test"
    model._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions())
    )
    model._usage = Usage()
    output = [part async for part in model.stream([{"role": "user", "content": "hello"}])]

    assert output == ["hello"]
    assert kwargs["stream_options"] == {"include_usage": True}
    assert model.usage() == Usage(input_tokens=8, output_tokens=2)


class _SummaryModel:
    name = "openai"
    model = "gpt-4o-mini"

    def __init__(self):
        self.calls = []

    async def complete(self, messages: list[dict], *, schema: dict | None = None):
        self.calls.append(messages)
        text = "Useful summary: " + "x" * 2_200 if len(self.calls) == 1 else "Final answer"
        return Completion(
            text=text, provider=self.name, model=self.model,
            usage=Usage(input_tokens=13, output_tokens=5),
        )


async def _seed_truncated_history(
    user_id: UUID, conversation_id: str, secret: str, *,
    count: int = 90, content_chars: int = 850,
) -> None:
    start = datetime.now(UTC) - timedelta(days=1)
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        for index in range(count):
            session.add(
                Message(
                    id=uuid.uuid4(), conversation_id=UUID(conversation_id), user_id=user_id,
                    role="user" if index % 2 == 0 else "assistant",
                    content=f"Historical decision {index}: " + "x" * content_chars,
                    content_type="text", created_at=start + timedelta(seconds=index),
                )
            )
        session.add(
            Memory(
                id=uuid.uuid4(), user_id=user_id, category="private", content=secret,
                status="confirmed", privacy_class="sensitive", active=True,
            )
        )
        await session.commit()


async def _no_plan(_model, _message):
    return Plan(
        intent="conversation", requires_execution=False, agents=[], risk="low",
        requires_approval=False, steps=[], direct_answer=None,
    ), None


def _assert_summary_is_bounded_and_memory_free(model: _SummaryModel, secret: str) -> None:
    prompt = "\n".join(message["content"] for message in model.calls[0])
    assert "Historical decision" in prompt
    assert secret not in prompt
    assert len(prompt) < 27_000
    context = "\n".join(message["content"] for message in model.calls[1])
    assert "Useful summary:" in context
    assert "jarvis-summary:v1|" not in context


@pytest.mark.asyncio
async def test_context_token_budget_triggers_summary_below_history_count(client, monkeypatch):
    headers = await _user(client)
    user_id = _user_id(headers)
    conversation_id = await _create_conversation(client, headers)
    secret = "sensitive-memory-content-not-for-summary"
    await _seed_truncated_history(
        user_id, conversation_id, secret, count=30, content_chars=1_200
    )
    model = _SummaryModel()
    monkeypatch.setattr("app.routers.conversations.get_model", lambda: model)
    monkeypatch.setattr("app.routers.conversations.plan_request", _no_plan)

    reply = await client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers=headers, json={"content": "What did we decide?"},
    )
    assert reply.status_code == 201, reply.text
    assert len(model.calls) == 2
    _assert_summary_is_bounded_and_memory_free(model, secret)


def test_retained_history_handles_tied_timestamps_and_partial_groups():
    timestamp = datetime.now(UTC)
    tied_rows = [
        SimpleNamespace(created_at=timestamp, role="assistant", content="answer"),
        SimpleNamespace(created_at=timestamp, role="user", content="question"),
    ]
    full_context = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    partial_context = [{"role": "assistant", "content": "answer"}]

    assert _retained_history_count(full_context, tied_rows) == (2, False)
    assert _retained_history_count(partial_context, tied_rows) == (1, True)


class _WriteSession:
    def __init__(self, row=None):
        self.row = row
        self.added = []
        self.commits = 0

    async def get(self, _model, _row_id):
        return self.row

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        now = datetime.now(UTC)
        for item in self.added:
            if isinstance(item, conversation_router.Conversation):
                item.created_at = item.created_at or now
                item.updated_at = item.updated_at or now
            if isinstance(item, conversation_router.Message):
                item.created_at = item.created_at or now

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_conversation_mutations_reject_before_writing_when_limited(monkeypatch):
    user_id = uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(), user_id=user_id, deleted_at=None, title="Original",
        status="active", created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    session = _WriteSession(row)
    calls = []

    async def limited(account_id):
        calls.append(account_id)
        raise HTTPException(429, "limited")

    monkeypatch.setattr(conversation_router, "limit_writes_durable", limited)
    user = SimpleNamespace(id=user_id)
    attempts = [
        conversation_router.create(conversation_router.ConversationIn(), user, session),
        conversation_router.update(
            row.id, conversation_router.ConversationPatch(title="Changed"), user, session
        ),
        conversation_router.delete(row.id, user, session),
        conversation_router.add_message(
            row.id, conversation_router.MessageIn(content="message"), user, session
        ),
        conversation_router.stream(
            row.id, conversation_router.MessageIn(content="stream"), user, session
        ),
    ]
    for attempt in attempts:
        with pytest.raises(HTTPException) as caught:
            await attempt
        assert caught.value.status_code == 429

    assert calls == [user_id] * 5
    assert session.added == []
    assert session.commits == 0
    assert row.title == "Original"
    assert row.deleted_at is None


@pytest.mark.asyncio
async def test_conversation_create_limit_is_owner_scoped(monkeypatch):
    counts = {}
    calls = []

    async def limit(account_id):
        calls.append(account_id)
        if counts.get(account_id, 0) >= 1:
            raise HTTPException(429, "limited")
        counts[account_id] = counts.get(account_id, 0) + 1

    monkeypatch.setattr(conversation_router, "limit_writes_durable", limit)
    owner_id, other_id = uuid.uuid4(), uuid.uuid4()
    owner = SimpleNamespace(id=owner_id)
    other = SimpleNamespace(id=other_id)

    created = await conversation_router.create(
        conversation_router.ConversationIn(), owner, _WriteSession()
    )
    with pytest.raises(HTTPException) as caught:
        await conversation_router.create(
            conversation_router.ConversationIn(), owner, _WriteSession()
        )
    other_created = await conversation_router.create(
        conversation_router.ConversationIn(), other, _WriteSession()
    )

    assert created["id"] != other_created["id"]
    assert caught.value.status_code == 429
    assert calls == [owner_id, owner_id, other_id]


@pytest.mark.asyncio
async def test_stream_is_limited_before_response_and_not_again_during_sse(monkeypatch):
    user_id = uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(), user_id=user_id, deleted_at=None, title="Chat",
        status="active", created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
    )
    calls = []

    async def allow(account_id):
        calls.append(account_id)

    async def events(*_args):
        yield "event: response.started\ndata: {}\n\n"

    monkeypatch.setattr(conversation_router, "limit_writes_durable", allow)
    monkeypatch.setattr(conversation_router, "_stream_events", events)
    response = await conversation_router.stream(
        row.id, conversation_router.MessageIn(content="stream"),
        SimpleNamespace(id=user_id), _WriteSession(row),
    )

    assert calls == [user_id]
    assert "response.started" in await anext(response.body_iterator)
    assert calls == [user_id]
