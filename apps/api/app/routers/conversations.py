"""Conversation CRUD and streaming replies."""

import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.base import NotConfigured, ProviderError
from app.ai.provider_factory import get_model
from app.ai.schemas import Plan
from app.ai.supervisor import plan_request
from app.integrations.requirements import missing_connection
from app.ai.usage import estimate_cost_usd
from app.observe import record_usage
from app.context import build_context
from app.db import get_session, session_scope
from app.models import Conversation, Message, ModelUsage, User
from app.security import error, require_user
from app.tasks_service import create_task_from_plan

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


class ConversationIn(BaseModel):
    title: str = Field(default="New conversation", max_length=200)


class ConversationPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    status: str | None = None


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


def _not_found() -> HTTPException:
    return HTTPException(
        404, error("not_found", "No conversation with that id exists for this account.")
    )


async def _owned(session: AsyncSession, conversation_id: uuid.UUID, user: User) -> Conversation:
    row = await session.get(Conversation, conversation_id)
    if row is None or row.user_id != user.id or row.deleted_at is not None:
        raise _not_found()
    return row


def _dump(row: Conversation) -> dict:
    return {
        "id": str(row.id),
        "title": row.title,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


@router.post("", status_code=201)
async def create(
    body: ConversationIn,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = Conversation(id=uuid.uuid4(), user_id=user.id, title=body.title, status="active")
    session.add(row)
    await session.commit()
    return _dump(row)


@router.get("")
async def list_conversations(
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    limit: int = 20,
    offset: int = 0,
) -> dict:
    limit = max(1, min(limit, 100))
    rows = (
        (
            await session.execute(
                select(Conversation)
                .where(Conversation.user_id == user.id, Conversation.deleted_at.is_(None))
                .order_by(Conversation.updated_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    total = (
        await session.execute(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.user_id == user.id, Conversation.deleted_at.is_(None))
        )
    ).scalar_one()
    return {"conversations": [_dump(row) for row in rows], "total": total}


@router.get("/{conversation_id}")
async def read(
    conversation_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return _dump(await _owned(session, conversation_id, user))


@router.patch("/{conversation_id}")
async def update(
    conversation_id: uuid.UUID,
    body: ConversationPatch,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _owned(session, conversation_id, user)
    if body.title is not None:
        row.title = body.title
    if body.status is not None:
        if body.status not in ("active", "archived"):
            raise HTTPException(422, error("invalid_status", "Status must be active or archived."))
        row.status = body.status
    row.updated_at = datetime.now(UTC)
    await session.commit()
    return _dump(row)


@router.delete("/{conversation_id}")
async def delete(
    conversation_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _owned(session, conversation_id, user)
    row.deleted_at = datetime.now(UTC)
    await session.commit()
    return {"deleted": True}


@router.get("/{conversation_id}/messages")
async def list_messages(
    conversation_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    limit: int = 50,
    offset: int = 0,
) -> dict:
    await _owned(session, conversation_id, user)
    rows = (
        (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at)
                .limit(max(1, min(limit, 200)))
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return {"messages": [_message(row) for row in rows]}


@router.post("/{conversation_id}/messages", status_code=201)
async def add_message(
    conversation_id: uuid.UUID,
    body: MessageIn,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Store the user's message and reply without streaming."""
    row = await _owned(session, conversation_id, user)
    session.add(_user_message(row, body.content))
    row.updated_at = datetime.now(UTC)
    await session.commit()

    try:
        reply, usage, plan, usage_row = await _reply(session, row)
    except HTTPException as exc:
        await session.commit()
        await _save_reply(row, _failure_note(exc), None)
        raise
    saved = await _save_reply(row, reply, usage)
    task_id = await _maybe_task(session, row, plan, usage_row)
    return _message(saved) | {"task_id": str(task_id) if task_id else None}


@router.post("/{conversation_id}/stream")
async def stream(
    conversation_id: uuid.UUID,
    body: MessageIn,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    row = await _owned(session, conversation_id, user)
    session.add(_user_message(row, body.content))
    row.updated_at = datetime.now(UTC)
    await session.commit()
    # The streaming response outlives this dependency's session.
    conversation_id = row.id

    async def events():
        yield _event("response.started", {})
        try:
            model = get_model()
        except NotConfigured:
            yield _event(
                "error",
                {"code": "ai_not_configured", "message": "No model provider is configured."},
            )
            return
        text = ""
        try:
            async with session_scope() as stream_session:
                conversation = await stream_session.get(Conversation, conversation_id)
                context = await build_context(stream_session, conversation)
            async for delta in model.stream(context):
                text += delta
                yield _event("text.delta", {"text": delta})
        except ProviderError as exc:
            yield _event("error", {"code": "provider_error", "message": str(exc)})
            return
        yield _event("response.completed", {"text": text})
        # Persisted after the final event. If the client disconnects before
        # this point the reply is lost; the user's message is not.
        async with session_scope() as stream_session:
            conversation = await stream_session.get(Conversation, conversation_id)
            await _save_reply(conversation, text, None, stream_session)

    return StreamingResponse(events(), media_type="text/event-stream")


def _user_message(conversation: Conversation, content: str) -> Message:
    return Message(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        user_id=conversation.user_id,
        role="user",
        content=content,
        content_type="text",
    )


def _failure_note(exc: HTTPException) -> str:
    detail: dict = exc.detail if isinstance(exc.detail, dict) else {}
    code = detail.get("error", {}).get("code", "error")
    return f"No reply was produced ({code})."


async def _reply(
    session: AsyncSession, conversation: Conversation
) -> tuple[str, dict | None, Plan, ModelUsage | None]:
    try:
        model = get_model()
    except NotConfigured:
        raise HTTPException(
            503, error("ai_not_configured", "No model provider is configured. Set OPENAI_API_KEY.")
        )
    context = await build_context(session, conversation)
    plan, plan_usage = await plan_request(model, context[-1]["content"])
    usage_row = None
    if plan_usage is not None:
        usage_row = await record_usage(
            session,
            user_id=conversation.user_id,
            task_id=None,
            provider=model.name,
            model=model.model,
            usage=plan_usage,
        )
    missing = (
        await missing_connection(session, conversation.user_id, plan.agents)
        if plan.requires_execution
        else None
    )
    if missing and plan.requires_approval:
        name = missing.replace("_", " ")
        return (
            f"This needs your approval, and {name} is not connected. I have not started it.",
            None,
            plan,
            usage_row,
        )
    if missing:
        return (
            f"Connect {missing.replace('_', ' ')} before I can do that. I have not started it.",
            None,
            plan,
            usage_row,
        )
    if plan.requires_execution and plan.requires_approval:
        summary = ", ".join(step.agent for step in plan.steps)
        return (
            f"This plan needs approval before it can run ({summary}). I have not started it.",
            None,
            plan,
            usage_row,
        )
    if plan.requires_execution:
        summary = ", ".join(step.agent for step in plan.steps)
        return f"I queued a plan using {summary}.", None, plan, usage_row
    completion = await model.complete(context)
    await record_usage(
        session,
        user_id=conversation.user_id,
        task_id=None,
        provider=completion.provider,
        model=completion.model,
        usage=completion.usage,
    )
    usage = {
        "input_tokens": completion.usage.input_tokens,
        "output_tokens": completion.usage.output_tokens,
        "estimated_cost_usd": estimate_cost_usd(
            completion.model, completion.usage.input_tokens, completion.usage.output_tokens
        ),
    }
    return completion.text, usage, plan, usage_row


async def _save_reply(
    conversation, text: str, usage: dict | None, session: AsyncSession | None = None
) -> Message:
    owns_session = session is None
    if session is None:
        session = session_scope()
    message = Message(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        user_id=conversation.user_id,
        role="assistant",
        content=text,
        content_type="text",
        token_usage=usage,
        provider="openai" if usage else None,
        model=None,
    )
    session.add(message)
    await session.commit()
    if owns_session:
        await session.close()
    return message


async def _maybe_task(
    session: AsyncSession,
    conversation: Conversation,
    plan: Plan,
    usage_row: ModelUsage | None = None,
) -> uuid.UUID | None:
    if plan is None or not plan.requires_execution:
        if usage_row is not None:
            await session.commit()
        return None
    # The reply already refused this. Do not queue it behind that refusal.
    missing = await missing_connection(session, conversation.user_id, plan.agents)
    if missing and not plan.requires_approval:
        if usage_row is not None:
            await session.commit()
        return None
    task_id = await create_task_from_plan(
        session,
        conversation.user_id,
        conversation.id,
        plan,
        enqueue=not plan.requires_approval and not missing,
        commit=usage_row is None,
    )
    if usage_row is not None:
        usage_row.task_id = task_id
        await session.commit()
    return task_id


def _message(row: Message) -> dict:
    return {
        "id": str(row.id),
        "role": row.role,
        "content": row.content,
        "content_type": row.content_type,
        "token_usage": row.token_usage,
        "created_at": row.created_at.isoformat(),
    }


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"
