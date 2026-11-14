"""Due briefings become in-app notifications. Nothing is emailed.

A schedule fires once per local calendar day, at or after its hour and minute.
The slot is claimed in the same transaction as the insert, so two workers
cannot both deliver it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.notifications import local_now, publish

BRIEFING_NAME = "daily_briefing"


def due(hour: int, minute: int, timezone: str, now: datetime | None = None) -> str | None:
    """The local date slot if the schedule's time has passed, else None."""
    local = local_now(timezone, now)
    if (local.hour, local.minute) < (hour, minute):
        return None
    return local.date().isoformat()


async def ensure_default(session: AsyncSession, user_id: uuid.UUID, timezone: str = "UTC") -> None:
    """Insert the briefing schedule if missing. A concurrent insert is not an error."""
    await session.execute(
        text(
            """
            INSERT INTO automations (id, user_id, name, hour, minute, timezone, enabled)
            VALUES (:id, :user_id, :name, 8, 0, :timezone, true)
            ON CONFLICT (user_id, name) DO NOTHING
            """
        ),
        {"id": uuid.uuid4(), "user_id": user_id, "name": BRIEFING_NAME, "timezone": timezone},
    )


async def run_due(session: AsyncSession, now: datetime | None = None) -> int:
    """Write one notification per due schedule. Caller commits.

    The slot is claimed before the insert. A second worker sees the claim and
    skips the row, so the same local day is not delivered twice.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT id, user_id, name, body, hour, minute, timezone, last_slot
                FROM automations
                WHERE enabled = true
                ORDER BY user_id, id
                FOR UPDATE
                """
            )
        )
    ).mappings().all()
    written = 0
    for row in rows:
        slot = due(row["hour"], row["minute"], row["timezone"], now)
        if slot is None or row["last_slot"] == slot:
            continue
        savepoint = await session.begin_nested()
        claimed = (
            await session.execute(
                text(
                    """
                    UPDATE automations
                    SET last_slot = :slot, updated_at = now()
                    WHERE id = :id AND user_id = :user_id
                      AND enabled = true
                      AND (last_slot IS DISTINCT FROM :slot)
                    RETURNING id
                    """
                ),
                {"slot": slot, "id": row["id"], "user_id": row["user_id"]},
            )
        ).scalar_one_or_none()
        if claimed is None:
            await savepoint.rollback()
            continue
        title, body, kind = await _copy(session, row, slot)
        note = await publish(
            session,
            user_id=row["user_id"],
            kind=kind,
            title=title,
            body=body,
            dedupe_key=f"{row['name']}:{slot}",
        )
        if note is None:
            await savepoint.rollback()
            continue
        await savepoint.commit()
        written += 1
    return written


async def _copy(session: AsyncSession, row, slot: str) -> tuple[str, str, str]:
    if row["name"] == BRIEFING_NAME:
        return "Daily briefing", await _body(session, row["user_id"], slot), "briefing"
    text_body = (row["body"] or "").strip() or row["name"]
    return row["name"][:80], text_body[:500], "automation"


async def _body(session: AsyncSession, user_id: uuid.UUID, slot: str) -> str:
    titles = (
        await session.execute(
            text(
                """
                SELECT title FROM tasks
                WHERE user_id = :user_id
                  AND status NOT IN ('completed', 'failed', 'cancelled')
                ORDER BY created_at
                LIMIT 5
                """
            ),
            {"user_id": user_id},
        )
    ).scalars().all()
    open_tasks = ", ".join(titles) if titles else "none"
    return (
        f"Briefing for {slot}. Open tasks: {open_tasks}. "
        "No mailbox or calendar was read. Email delivery is not configured."
    )[:500]
