"""Cursor-based synchronization.

Events are durable rows, not an in-memory stream. A client passes the last id it
has seen and receives everything newer, scoped to its own user. Reconnecting
after a gap replays the missed events, and asking twice returns the same rows.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.security import require_user

router = APIRouter(prefix="/api/v1/sync", tags=["sync"])


@router.get("")
async def sync(
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    after: int = 0,
    limit: int = 100,
) -> dict:
    limit = max(1, min(limit, 500))
    rows = (
        await session.execute(
            text(
                """
                SELECT id, kind, device_id, payload, created_at
                FROM sync_events
                WHERE user_id = :user_id AND id > :after AND kind <> 'command.idempotency'
                ORDER BY id
                LIMIT :limit
                """
            ),
            {"user_id": user.id, "after": after, "limit": limit},
        )
    ).mappings().all()
    events = [
        {
            "id": row["id"],
            "kind": row["kind"],
            "device_id": str(row["device_id"]) if row["device_id"] else None,
            "payload": row["payload"],
            "created_at": row["created_at"].isoformat(),
        }
        for row in rows
    ]
    cursor = events[-1]["id"] if events else after
    return {"events": events, "cursor": cursor}
