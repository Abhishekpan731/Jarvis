"""The operator view returns counts. It does not return user content."""

import uuid
from unittest.mock import AsyncMock, Mock

import pytest
from app import observe
from app.ai.schemas import Usage
from app.ai.usage import estimate_cost_usd
from app.config import settings
from app.db import session_scope, set_tenant_guc, worker_session_scope
from app.models import Conversation, Message, User
from app.observe import queue_depth, record_usage, usage_totals
from app.routers import admin as admin_router
from app.security import hash_password
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


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
    async with session_scope() as totals_session:
        before = await usage_totals(totals_session)
    async with session_scope() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("correct horse battery"),
            display_name="Test",
        )
        session.add(user)
        await session.flush()
        await set_tenant_guc(session, user.id)
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
    async with worker_session_scope() as worker_session:
        depth = await queue_depth(worker_session)

    expected_cost = estimate_cost_usd("gpt-4o-mini", 10, 4)
    assert totals["input_tokens"] == before["input_tokens"] + 10
    assert totals["output_tokens"] == before["output_tokens"] + 4
    assert totals["calls"] == before["calls"] + 1
    assert totals["estimated_cost_usd"] == pytest.approx(before["estimated_cost_usd"] + expected_cost)

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


@pytest.mark.asyncio
async def test_admin_overview_uses_aggregate_queue_counts(monkeypatch):
    queue_result = Mock()
    queue_result.mappings.return_value.one.return_value = {
        "queued": 4,
        "running": 2,
        "retry_wait": 1,
        "failed": 3,
    }
    session = Mock()
    session.execute = AsyncMock(return_value=queue_result)
    monkeypatch.setattr(
        admin_router,
        "usage_totals",
        AsyncMock(return_value={"calls": 5}),
    )

    response = await admin_router.overview(None, session)

    assert response["queue"] == {
        "queued": 4,
        "running": 2,
        "retry_wait": 1,
        "failed": 3,
    }
    assert session.execute.await_count == 1
    assert str(session.execute.await_args.args[0]) == (
        "SELECT * FROM public.admin_queue_depth()"
    )


@pytest.mark.asyncio
async def test_admin_guard_rejects_untrusted_token(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "configured-admin-token")
    credentials = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials="untrusted-token"
    )

    with pytest.raises(HTTPException) as error:
        await admin_router.require_admin(credentials)

    assert error.value.status_code == 401


@pytest.fixture
def memory_span_exporter(monkeypatch):
    observe.shutdown_telemetry()
    exporter = InMemorySpanExporter()
    exporter_factory = Mock(return_value=exporter)
    monkeypatch.setattr(observe, "OTLPSpanExporter", exporter_factory)
    monkeypatch.setattr(settings, "otel_exporter_otlp_endpoint", "http://collector.invalid")
    yield exporter, exporter_factory
    observe.shutdown_telemetry()


def test_otlp_tracing_is_disabled_without_an_endpoint(monkeypatch):
    exporter_factory = Mock()
    monkeypatch.setattr(observe, "OTLPSpanExporter", exporter_factory)
    monkeypatch.setattr(settings, "otel_exporter_otlp_endpoint", "")

    observe.trace("model_usage", input_tokens=12)

    exporter_factory.assert_not_called()
    assert observe._tracer_provider is None


def test_otlp_tracing_exports_a_fixed_usage_span(memory_span_exporter):
    exporter, exporter_factory = memory_span_exporter

    observe.trace("model_usage", input_tokens=12, output_tokens=5)
    assert observe._tracer_provider.force_flush()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "model_usage"
    assert spans[0].attributes == {}
    exporter_factory.assert_called_once_with(endpoint="http://collector.invalid/v1/traces")


def test_otlp_spans_exclude_sensitive_and_dynamic_fields(memory_span_exporter, monkeypatch):
    exporter, _ = memory_span_exporter
    monkeypatch.setattr(observe.log, "info", Mock())

    observe.trace(
        "model_usage",
        prompt="private prompt marker",
        memory_text="private memory marker",
        access_token="private token marker",
        user_id="private user marker",
    )
    assert observe._tracer_provider.force_flush()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes == {}
    assert spans[0].resource.attributes == {"service.name": "jarvis-api"}
    assert spans[0].name == "model_usage"


def test_otlp_provider_flushes_and_shuts_down(memory_span_exporter, monkeypatch):
    exporter, _ = memory_span_exporter
    exporter_shutdown = Mock(wraps=exporter.shutdown)
    monkeypatch.setattr(exporter, "shutdown", exporter_shutdown)
    observe.trace("model_usage", input_tokens=12)

    observe.shutdown_telemetry()

    assert exporter.get_finished_spans()
    exporter_shutdown.assert_called_once()
    assert observe._tracer_provider is None
