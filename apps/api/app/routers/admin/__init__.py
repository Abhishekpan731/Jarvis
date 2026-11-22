"""Operator view. Counts only. No conversation, memory, or instruction text.

A user JWT does not open this route. The query selects status and sums, never
a payload, a message, or an email address.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.observe import usage_totals
from app.security import require_admin

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/overview")
async def overview(
    _: None = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    result = await session.execute(text("SELECT * FROM public.admin_queue_depth()"))
    depth = dict(result.mappings().one())
    usage = await usage_totals(session)
    return {
        "queue": depth,
        "usage": usage,
        "content": None,
    }
