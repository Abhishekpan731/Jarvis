"""Operator view. Counts only. No conversation, memory, or instruction text.

A user JWT does not open this route. The query selects status and sums, never
a payload, a message, or an email address.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session, set_worker_guc
from app.observe import queue_depth, usage_totals
from app.security import require_admin

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/overview")
async def overview(
    _: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    # jobs is RLS-forced and tenant-scoped; the operator view aggregates
    # across every tenant, so it needs the worker escape hatch.
    await set_worker_guc(session)
    depth = await queue_depth(session)
    usage = await usage_totals(session)
    return {
        "queue": depth,
        "usage": usage,
        "content": None,
    }
