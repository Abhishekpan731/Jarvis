"""Write an in-app notification. This does not send email or push to a device.

A dedupe key makes a second insert of the same fact a no-op, so a restarted
worker cannot deliver the same briefing twice.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

KINDS = frozenset({"briefing", "task", "automation"})


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return False
    return True


def local_now(name: str, now: datetime | None = None) -> datetime:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    try:
        zone = ZoneInfo(name)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    return moment.astimezone(zone)


async def publish(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    kind: str,
    title: str,
    body: str,
    dedupe_key: str | None = None,
) -> uuid.UUID | None:
    """Insert one unread row. Returns None when the dedupe key already exists.

    A missing key is not a dedupe. PostgreSQL treats NULL as distinct, so the
    conflict target would never match and a retry would insert again.
    """
    if kind not in KINDS:
        raise ValueError("unknown notification kind")
    if not dedupe_key:
        raise ValueError("dedupe key is required")
    note_id = uuid.uuid4()
    inserted = (
        await session.execute(
            text(
                """
                INSERT INTO notifications (id, user_id, kind, title, body, channel, dedupe_key)
                VALUES (:id, :user_id, :kind, :title, :body, 'in_app', :dedupe_key)
                ON CONFLICT (user_id, dedupe_key) DO NOTHING
                RETURNING id
                """
            ),
            {
                "id": note_id,
                "user_id": user_id,
                "kind": kind,
                "title": title[:200],
                "body": body[:4000],
                "dedupe_key": dedupe_key,
            },
        )
    ).scalar_one_or_none()
    return inserted
