"""The command gateway. One entry point for every interaction surface.

Siri, Google Assistant, web, desktop, and direct API calls all submit the same
contract and reach the same planner. A device id is recorded for attribution; it
is never treated as authentication. Long work is persisted and queued, and the
caller gets an acknowledgement immediately rather than waiting on the worker.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from jarvis_shared.commands import (
    INPUT_TYPES,
    SOURCES,
    device_assistant,
    handoff_reply,
    normalize_command,
    voice_reply,
)
from jarvis_shared.speech import normalize_locale
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.provider_factory import NotConfigured, get_model
from app.ai.supervisor import plan_request
from app.db import get_session
from app.integrations.requirements import missing_connection
from app.limits import limit_writes_durable
from app.models import Conversation, Device, Message, SyncEvent, User
from app.observe import record_usage
from app.security import error, require_user
from app.tasks_service import create_task_from_plan

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])


class CommandIn(BaseModel):
    command: str = Field(min_length=1, max_length=20_000)
    source: str = "api"
    device_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    input_type: str = "text"
    locale: str = Field(default="en", max_length=16)


def _check(body: CommandIn) -> str:
    if body.source not in SOURCES:
        raise HTTPException(422, error("invalid_source", "Unknown command source."))
    if body.input_type not in INPUT_TYPES:
        raise HTTPException(422, error("invalid_input_type", "Input type must be voice or text."))
    if body.input_type == "voice":
        try:
            normalize_locale(body.locale)
        except ValueError:
            raise HTTPException(
                422, error("unsupported_locale", "Voice supports English (en) and Hindi (hi).")
            ) from None
    command = normalize_command(body.command)
    if not command:
        raise HTTPException(422, error("empty_command", "The command was empty after normalization."))
    return command


async def _device(session: AsyncSession, user: User, device_id: uuid.UUID | None) -> Device | None:
    """Confirm the device belongs to this user. A foreign or revoked id is rejected."""
    if device_id is None:
        return None
    device = await session.get(Device, device_id)
    if device is None or device.user_id != user.id:
        raise HTTPException(404, error("device_not_found", "No device with that id exists for this account."))
    if device.revoked_at is not None:
        raise HTTPException(403, error("device_revoked", "This device has been revoked."))
    device.last_seen_at = datetime.now(UTC)
    return device


async def _conversation(
    session: AsyncSession, user: User, conversation_id: uuid.UUID | None,
    device: Device | None = None,
) -> Conversation:
    if conversation_id is not None:
        row = await session.get(Conversation, conversation_id)
        if row is None or row.user_id != user.id or row.deleted_at is not None:
            raise HTTPException(
                404, error("not_found", "No conversation with that id exists for this account.")
            )
        return row
    row = Conversation(id=uuid.uuid4(), user_id=user.id, title="Assistant", status="active")
    session.add(row)
    await session.flush()
    await _record(
        session, user, device, "conversation.created",
        {"conversation": _conversation_payload(row)},
    )
    return row


async def _record(
    session: AsyncSession, user: User, device: Device | None, kind: str, payload: dict
) -> None:
    session.add(
        SyncEvent(
            user_id=user.id,
            device_id=device.id if device else None,
            kind=kind,
            payload=payload,
        )
    )


def _conversation_payload(conversation: Conversation) -> dict:
    """Serialize conversation metadata without message content."""
    return {
        "id": str(conversation.id),
        "title": conversation.title,
        "status": conversation.status,
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
    }


async def _record_message(
    session: AsyncSession, user: User, device: Device | None,
    conversation: Conversation, message: Message,
) -> None:
    """Flush a message before publishing its owner-scoped sync event."""
    await session.flush()
    await _record(
        session, user, device, "message.created",
        {
            "conversation_id": str(conversation.id),
            "message": {
                "id": str(message.id),
                "role": message.role,
                "content": message.content,
                "content_type": message.content_type,
                "created_at": message.created_at.isoformat(),
            },
        },
    )


@router.post("/commands", status_code=202)
async def submit_command(
    body: CommandIn,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Submit one command; an existing owner-scoped replay is free."""
    command = _check(body)
    device = await _device(session, user, body.device_id)

    if idempotency_key:
        prior = await _replay(session, user.id, idempotency_key)
        if prior is not None:
            return prior

    # Charge only new submissions; retries replay before consuming another slot.
    await limit_writes_durable(user.id)

    conversation = await _conversation(session, user, body.conversation_id, device)
    conversation.updated_at = datetime.now(UTC)
    await _record(
        session, user, device, "conversation.updated",
        {"conversation": _conversation_payload(conversation)},
    )
    user_message = Message(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        user_id=user.id,
        role="user",
        content=command,
        content_type="text",
    )
    session.add(user_message)
    await _record_message(session, user, device, conversation, user_message)

    try:
        model = get_model()
    except NotConfigured:
        model = None
    plan, usage = await plan_request(model, command)
    usage_row = None
    if usage is not None and model is not None:
        usage_row = await record_usage(
            session,
            user_id=user.id,
            task_id=None,
            provider=model.name,
            model=model.model,
            usage=usage,
        )
    missing = await missing_connection(session, user.id, plan.agents) if plan.requires_execution else None

    task_id = None
    if plan.requires_execution and (plan.requires_approval or not missing):
        # Approval still records a waiting task. A missing connection never queues one.
        # The caller commits, so the token count can name this task in the same write.
        task_id = await create_task_from_plan(
            session,
            user.id,
            conversation.id,
            plan,
            enqueue=not plan.requires_approval and not missing,
            commit=False,
        )
        if usage_row is not None:
            usage_row.task_id = task_id

    assistant = _device_handoff(body.source, model, plan)
    reply = handoff_reply(assistant) if assistant else _reply_text(plan, missing)
    assistant_message = Message(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        user_id=user.id,
        role="assistant",
        content=reply,
        content_type="text",
    )
    session.add(assistant_message)
    await _record_message(session, user, device, conversation, assistant_message)
    result = {
        "request_id": idempotency_key or str(uuid.uuid4()),
        "conversation_id": str(conversation.id),
        "task_id": str(task_id) if task_id else None,
        "status": "handed_off" if assistant else _status(plan),
        "response_text": reply,
        "voice_response": voice_reply(reply),
        "requires_approval": False if assistant else plan.requires_approval,
        "source": body.source,
        "handoff": assistant is not None,
        "device_assistant": body.source if assistant else None,
    }
    kind = "command.handed_off" if assistant else "command.accepted"
    await _record(session, user, device, kind, result)
    if idempotency_key:
        await _remember(session, user.id, idempotency_key, result)
    await session.commit()
    return result


def _status(plan) -> str:
    if plan.requires_approval:
        return "waiting_for_approval"
    return "accepted"


def _device_handoff(source: str, model, plan) -> str | None:
    """Hand a conversational request back only when this device can continue it.

    An executable keyword plan still queues. Web, desktop, and api have no
    on-device assistant to hand the request to.
    """
    if model is not None or plan.requires_execution:
        return None
    return device_assistant(source)



def _reply_text(plan, missing: str | None = None) -> str:
    if missing and plan.requires_approval:
        name = missing.replace("_", " ")
        return f"This needs your approval, and {name} is not connected. I have not started it."
    if missing:
        return f"Connect {missing.replace('_', ' ')} before I can do that. I have not started it."
    if not plan.requires_execution:
        return plan.direct_answer or "I noted that. Nothing needed to be scheduled."
    agents = ", ".join(plan.agents) or "an agent"
    if plan.requires_approval:
        return f"This needs your approval before it runs ({agents}). I have not started it."
    return f"I queued that with {agents}. You can check its status from any device."


async def _replay(session: AsyncSession, user_id: uuid.UUID, key: str) -> dict | None:
    row = (
        await session.execute(
            text(
                """
                SELECT payload FROM sync_events
                WHERE user_id = :user_id AND kind = 'command.idempotency' AND payload->>'key' = :key
                ORDER BY id DESC LIMIT 1
                """
            ),
            {"user_id": user_id, "key": key},
        )
    ).mappings().one_or_none()
    if row is None:
        return None
    return row["payload"]["result"]


async def _remember(session: AsyncSession, user_id: uuid.UUID, key: str, result: dict) -> None:
    session.add(
        SyncEvent(
            user_id=user_id,
            kind="command.idempotency",
            payload={"key": key, "result": result},
        )
    )
