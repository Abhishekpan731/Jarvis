"""Database engine and session management.

The engine is built per event loop. asyncpg connections cannot move between
loops, and the test suite creates a new loop per test.
"""

import asyncio
import uuid
import weakref
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# Keyed by the loop object itself (weakly), not id(loop): CPython reuses freed
# memory addresses, so id(asyncio.get_running_loop()) can collide across
# pytest's per-test event loops once the previous loop is garbage collected.
# That collision returned a stale engine/pool bound to an already-closed
# loop, corrupting the asyncpg connection's protocol state under concurrent
# use. A WeakKeyDictionary keys on identity safely and drops the entry as
# soon as its loop is collected, so a reused address can never alias a dead
# loop's engine onto a new one.
_engines: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple]" = weakref.WeakKeyDictionary()


class Base(DeclarativeBase):
    pass


def _factory() -> async_sessionmaker:
    loop = asyncio.get_running_loop()
    cached = _engines.get(loop)
    if cached is None:
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        cached = (engine, async_sessionmaker(engine, expire_on_commit=False))
        _engines[loop] = cached
    return cached[1]


def session_scope() -> AsyncSession:
    """Open a session. Use as `async with session_scope() as session`."""
    return _factory()()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


async def set_tenant_guc(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Scope this transaction to one tenant. RLS policies read this GUC.

    `set_config(..., true)` is transaction-local, same as `SET LOCAL`, so the
    value never leaks across a pooled connection once the transaction ends.
    """
    await session.execute(text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)})


async def set_worker_guc(session: AsyncSession) -> None:
    """Mark this transaction as the durable worker's cross-tenant queue claim.

    Narrow, intentional bypass: only jobs/idempotency_keys/task_steps/task_events
    RLS policies honor it, and only for the lifetime of this transaction.
    """
    await session.execute(text("SELECT set_config('app.worker', 'true', true)"))
