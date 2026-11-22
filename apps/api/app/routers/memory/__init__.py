"""User-controlled memory. Nothing is stored unless the user asks."""

import asyncio
import json
import logging
import math
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.provider_factory import get_model
from app.db import get_session
from app.models import Memory, User
from app.security import error, require_user

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])
_EMBEDDING_MODEL = "text-embedding-3-small"
_EMBEDDING_DIMENSIONS = 1_536
_EMBEDDING_TIMEOUT_SECONDS = 8
_logger = logging.getLogger(__name__)

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
    privacy_class: str = "sensitive"


class MemoryPatch(BaseModel):
    content: str | None = Field(default=None, max_length=4_000)
    active: bool | None = None
    privacy_class: str | None = None


class MemoryConfirmation(BaseModel):
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
        "status": row.status,
        "privacy_class": row.privacy_class,
        "active": row.active,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


async def _create_embedding(content: str) -> list[float] | None:
    """Return a validated provider embedding, or None when unavailable."""
    try:
        async with asyncio.timeout(_EMBEDDING_TIMEOUT_SECONDS):
            model = get_model()
            embed = getattr(model, "embed", None)
            if callable(embed):
                values = await embed(content)
            else:
                client = getattr(model, "_client", None)
                create = getattr(getattr(client, "embeddings", None), "create", None)
                if not callable(create):
                    return None
                response = await create(
                    model=_EMBEDDING_MODEL,
                    input=content,
                    dimensions=_EMBEDDING_DIMENSIONS,
                )
                values = response.data[0].embedding
            vector = [float(value) for value in values]
            if len(vector) != _EMBEDDING_DIMENSIONS or not all(map(math.isfinite, vector)):
                return None
            return vector
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Memory embedding unavailable (%s)", type(exc).__name__)
        return None


def _vector_literal(vector: list[float]) -> str:
    return json.dumps(vector, separators=(",", ":"))


async def _set_embedding(row: Memory) -> None:
    """Embed only confirmed, active, normal-privacy content; clear all others."""
    if (
        row.status != "confirmed"
        or row.privacy_class != "normal"
        or not row.active
        or row.deleted_at is not None
    ):
        row.embedding = None
        return
    vector = await _create_embedding(row.content)
    row.embedding = _vector_literal(vector) if vector is not None else None


async def _owned_memory(
    session: AsyncSession, memory_id: uuid.UUID, user: User, *, include_deleted: bool = False
) -> Memory:
    """Return a non-deleted memory only when it belongs to the authenticated user."""
    row = await session.get(Memory, memory_id)
    if (
        row is None
        or row.user_id != user.id
        or (row.deleted_at is not None and not include_deleted)
    ):
        raise HTTPException(
            404, error("not_found", "No memory with that id exists for this account.")
        )
    return row


async def _semantic_search(
    session: AsyncSession, user_id: uuid.UUID, query: str
) -> list[Memory] | None:
    """Run bounded vector search when a validated embedding is available."""
    vector = await _create_embedding(query)
    if vector is None:
        return None
    statement = (
        select(Memory)
        .where(
            Memory.user_id == user_id,
            Memory.status == "confirmed",
            Memory.privacy_class == "normal",
            Memory.active.is_(True),
            Memory.deleted_at.is_(None),
            Memory.embedding.is_not(None),
        )
        .order_by(Memory.embedding.op("<=>")(text("CAST(:embedding AS vector)")))
        .limit(100)
    )
    result = await session.execute(statement, {"embedding": _vector_literal(vector)})
    return result.scalars().all()


@router.get("")
async def list_memories(
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    q: str | None = Query(default=None, max_length=1_000),
) -> dict:
    query = select(Memory).where(
        Memory.user_id == user.id,
        Memory.status == "confirmed",
        Memory.deleted_at.is_(None),
    )
    if q:
        semantic_rows = await _semantic_search(session, user.id, q)
        if semantic_rows:
            return {"memories": [_dump(row) for row in semantic_rows]}
        query = query.where(or_(Memory.content.ilike(f"%{q}%"), Memory.category == q))
    rows = (
        (await session.execute(query.order_by(Memory.updated_at.desc()).limit(100))).scalars().all()
    )
    return {"memories": [_dump(row) for row in rows]}


@router.post("", status_code=201)
async def create(
    body: MemoryIn,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check(body.category, body.privacy_class)
    now = datetime.now(UTC)
    row = Memory(
        id=uuid.uuid4(),
        user_id=user.id,
        category=body.category,
        content=body.content,
        status="proposed",
        privacy_class=body.privacy_class,
        active=False,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    return _dump(row)


@router.post("/{memory_id}/confirm")
async def confirm_memory(
    memory_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    body: MemoryConfirmation | None = None,
) -> dict:
    row = await _owned_memory(session, memory_id, user)
    if body is not None and body.privacy_class is not None:
        _check(row.category, body.privacy_class)
    if row.status == "proposed":
        if body is not None and body.privacy_class is not None:
            row.privacy_class = body.privacy_class
        row.status = "confirmed"
        row.active = True
        row.updated_at = datetime.now(UTC)
        await _set_embedding(row)
        await session.commit()
    return _dump(row)


@router.post("/{memory_id}/reject")
async def reject_memory(
    memory_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _owned_memory(session, memory_id, user, include_deleted=True)
    if row.deleted_at is None:
        row.active = False
        row.embedding = None
        row.deleted_at = datetime.now(UTC)
        row.updated_at = row.deleted_at
        await session.commit()
    return {"rejected": True}


@router.patch("/{memory_id}")
async def update(
    memory_id: uuid.UUID,
    body: MemoryPatch,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _owned_memory(session, memory_id, user)
    content_changed = body.content is not None and body.content != row.content
    privacy_changed = body.privacy_class is not None and body.privacy_class != row.privacy_class
    active_changed = body.active is not None and body.active != row.active
    if body.privacy_class is not None:
        _check(row.category, body.privacy_class)
        row.privacy_class = body.privacy_class
    if body.content is not None:
        row.content = body.content
    if body.active is not None:
        row.active = body.active
    if content_changed or privacy_changed or active_changed:
        await _set_embedding(row)
    row.updated_at = datetime.now(UTC)
    await session.commit()
    return _dump(row)


@router.delete("/{memory_id}")
async def delete(
    memory_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _owned_memory(session, memory_id, user, include_deleted=True)
    if row.deleted_at is None:
        row.active = False
        row.embedding = None
        row.deleted_at = datetime.now(UTC)
        row.updated_at = row.deleted_at
        await session.commit()
    return {"deleted": True}
