"""User-controlled memory. Nothing is stored unless the user asks."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Memory, User
from app.security import error, require_user

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])

CATEGORIES = (
    "profile",
    "preferences",
    "work",
    "projects",
    "goals",
    "knowledge",
    "decisions",
    "routines",
    "summaries",
    "instructions",
)


class MemoryIn(BaseModel):
    category: str
    content: str = Field(min_length=1, max_length=4_000)
    privacy_class: str = "normal"


class MemoryPatch(BaseModel):
    content: str | None = Field(default=None, max_length=4_000)
    active: bool | None = None
    privacy_class: str | None = None


def _check(category: str, privacy: str) -> None:
    if category not in CATEGORIES:
        raise HTTPException(422, error("invalid_category", "Unknown memory category."))
    if privacy not in ("normal", "sensitive"):
        raise HTTPException(422, error("invalid_privacy", "Privacy must be normal or sensitive."))


def _dump(row: Memory) -> dict:
    return {
        "id": str(row.id),
        "category": row.category,
        "content": row.content,
        "privacy_class": row.privacy_class,
        "active": row.active,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


@router.get("")
async def list_memories(
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    q: str | None = None,
) -> dict:
    query = select(Memory).where(Memory.user_id == user.id, Memory.deleted_at.is_(None))
    if q:
        query = query.where(or_(Memory.content.ilike(f"%{q}%"), Memory.category == q))
    rows = (await session.execute(query.order_by(Memory.updated_at.desc()).limit(100))).scalars()
    return {"memories": [_dump(row) for row in rows]}


@router.post("", status_code=201)
async def create(
    body: MemoryIn,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check(body.category, body.privacy_class)
    row = Memory(
        id=uuid.uuid4(),
        user_id=user.id,
        category=body.category,
        content=body.content,
        privacy_class=body.privacy_class,
        active=True,
    )
    session.add(row)
    await session.commit()
    return _dump(row)


@router.patch("/{memory_id}")
async def update(
    memory_id: uuid.UUID,
    body: MemoryPatch,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Memory, memory_id)
    if row is None or row.user_id != user.id or row.deleted_at is not None:
        raise HTTPException(
            404, error("not_found", "No memory with that id exists for this account.")
        )
    if body.privacy_class is not None:
        _check(row.category, body.privacy_class)
        row.privacy_class = body.privacy_class
    if body.content is not None:
        row.content = body.content
    if body.active is not None:
        row.active = body.active
    row.updated_at = datetime.now(UTC)
    await session.commit()
    return _dump(row)


@router.delete("/{memory_id}")
async def delete(
    memory_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(Memory, memory_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(
            404, error("not_found", "No memory with that id exists for this account.")
        )
    row.deleted_at = datetime.now(UTC)
    await session.commit()
    return {"deleted": True}
