"""In-app notifications and the daily briefing schedule.

Every query filters on the authenticated user. Browser notification permission
is a client concern; this API never claims a push was sent. Email is not a
channel until a mail credential exists.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.briefing import BRIEFING_NAME, ensure_default
from app.db import get_session
from app.models import User
from app.notifications import valid_timezone
from app.security import error, require_user

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


class ScheduleIn(BaseModel):
    hour: int = Field(ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    timezone: str = Field(default="UTC", max_length=64)
    enabled: bool = True

class ReminderIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    body: str = Field(default="", max_length=500)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    timezone: str = Field(default="UTC", max_length=64)


def _clean_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned or cleaned == BRIEFING_NAME:
        raise HTTPException(422, error("invalid_name", "Choose a different reminder name."))
    return cleaned


def _note(row) -> dict:
    return {
        "id": str(row["id"]),
        "kind": row["kind"],
        "title": row["title"],
        "body": row["body"],
        "channel": row["channel"],
        "read": row["read_at"] is not None,
        "created_at": row["created_at"].isoformat(),
    }


@router.get("")
async def list_notifications(
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    limit: int = 20,
) -> dict:
    limit = min(max(limit, 1), 50)
    rows = (
        await session.execute(
            text(
                """
                SELECT id, kind, title, body, channel, read_at, created_at
                FROM notifications
                WHERE user_id = :user_id
                ORDER BY created_at DESC, id DESC
                LIMIT :limit
                """
            ),
            {"user_id": user.id, "limit": limit},
        )
    ).mappings().all()
    unread = (
        await session.execute(
            text(
                """
                SELECT count(*) FROM notifications
                WHERE user_id = :user_id AND read_at IS NULL
                """
            ),
            {"user_id": user.id},
        )
    ).scalar_one()
    return {"notifications": [_note(row) for row in rows], "unread": unread}


@router.post("/{note_id}/read")
async def mark_read(
    note_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    updated = await session.execute(
        text(
            """
            UPDATE notifications
            SET read_at = now()
            WHERE id = :id AND user_id = :user_id AND read_at IS NULL
            RETURNING id
            """
        ),
        {"id": note_id, "user_id": user.id},
    )
    if updated.scalar_one_or_none() is None:
        exists = (
            await session.execute(
                text("SELECT 1 FROM notifications WHERE id = :id AND user_id = :user_id"),
                {"id": note_id, "user_id": user.id},
            )
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(404, error("not_found", "Notification not found."))
    await session.commit()
    return {"read": True}


@router.get("/schedule")
async def read_schedule(
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await ensure_default(session, user.id)
    await session.commit()
    return await _schedule(session, user.id)


@router.put("/schedule")
async def update_schedule(
    body: ScheduleIn,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not valid_timezone(body.timezone):
        raise HTTPException(422, error("invalid_timezone", "That timezone is not recognized."))
    await ensure_default(session, user.id, body.timezone)
    await session.execute(
        text(
            """
            UPDATE automations
            SET hour = :hour, minute = :minute, timezone = :timezone,
                enabled = :enabled, updated_at = :now
            WHERE user_id = :user_id AND name = :name
            """
        ),
        {
            "hour": body.hour,
            "minute": body.minute,
            "timezone": body.timezone,
            "enabled": body.enabled,
            "now": datetime.now(UTC),
            "user_id": user.id,
            "name": BRIEFING_NAME,
        },
    )
    await session.commit()
    return await _schedule(session, user.id)


async def _schedule(session: AsyncSession, user_id: uuid.UUID) -> dict:
    row = (
        await session.execute(
            text(
                """
                SELECT hour, minute, timezone, enabled, last_slot
                FROM automations
                WHERE user_id = :user_id AND name = :name
                """
            ),
            {"user_id": user_id, "name": BRIEFING_NAME},
        )
    ).mappings().one()
    return {
        "name": BRIEFING_NAME,
        "hour": row["hour"],
        "minute": row["minute"],
        "timezone": row["timezone"],
        "enabled": row["enabled"],
        "last_slot": row["last_slot"],
        "email_configured": False,
    }


@router.get("/reminders")
async def list_reminders(
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (
        await session.execute(
            text(
                """
                SELECT id, name, body, hour, minute, timezone, enabled
                FROM automations
                WHERE user_id = :user_id AND name <> :briefing
                ORDER BY name
                """
            ),
            {"user_id": user.id, "briefing": BRIEFING_NAME},
        )
    ).mappings().all()
    return {"reminders": [_reminder(row) for row in rows]}


@router.post("/reminders", status_code=201)
async def create_reminder(
    body: ReminderIn,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not valid_timezone(body.timezone):
        raise HTTPException(422, error("invalid_timezone", "That timezone is not recognized."))
    name = _clean_name(body.name)
    count = (
        await session.execute(
            text(
                """
                SELECT count(*) FROM automations
                WHERE user_id = :user_id AND name <> :briefing
                """
            ),
            {"user_id": user.id, "briefing": BRIEFING_NAME},
        )
    ).scalar_one()
    if count >= 20:
        raise HTTPException(422, error("limit_reached", "Twenty reminders is the limit."))
    inserted = (
        await session.execute(
            text(
                """
                INSERT INTO automations
                    (id, user_id, name, body, hour, minute, timezone, enabled)
                VALUES
                    (:id, :user_id, :name, :body, :hour, :minute, :timezone, true)
                ON CONFLICT (user_id, name) DO NOTHING
                RETURNING id, name, body, hour, minute, timezone, enabled
                """
            ),
            {
                "id": uuid.uuid4(),
                "user_id": user.id,
                "name": name,
                "body": body.body.strip(),
                "hour": body.hour,
                "minute": body.minute,
                "timezone": body.timezone,
            },
        )
    ).mappings().one_or_none()
    if inserted is None:
        raise HTTPException(409, error("name_taken", "A reminder with that name already exists."))
    await session.commit()
    return _reminder(inserted)


@router.delete("/reminders/{reminder_id}")
async def delete_reminder(
    reminder_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    deleted = (
        await session.execute(
            text(
                """
                DELETE FROM automations
                WHERE id = :id AND user_id = :user_id AND name <> :briefing
                RETURNING id
                """
            ),
            {"id": reminder_id, "user_id": user.id, "briefing": BRIEFING_NAME},
        )
    ).scalar_one_or_none()
    if deleted is None:
        raise HTTPException(404, error("not_found", "Reminder not found."))
    await session.commit()
    return {"deleted": True}


def _reminder(row) -> dict:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "body": row["body"],
        "hour": row["hour"],
        "minute": row["minute"],
        "timezone": row["timezone"],
        "enabled": row["enabled"],
    }
