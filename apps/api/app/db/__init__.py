"""Database engine and session management.

The engine is built per event loop. asyncpg connections cannot move between
loops, and the test suite creates a new loop per test.
"""

import asyncio
import uuid
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
_engines: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, tuple]]" = (
    weakref.WeakKeyDictionary()
)


class Base(DeclarativeBase):
    pass


def _factory(database_url: str) -> async_sessionmaker:
    loop = asyncio.get_running_loop()
    loop_engines = _engines.setdefault(loop, {})
    cached = loop_engines.get(database_url)
    if cached is None:
        engine = create_async_engine(database_url, pool_pre_ping=True)
        cached = (engine, async_sessionmaker(engine, expire_on_commit=False))
        loop_engines[database_url] = cached
    return cached[1]


@asynccontextmanager
async def _session_scope(database_url: str, expected_role: str) -> AsyncIterator[AsyncSession]:
    async with _factory(database_url)() as session:
        authenticated_role = await session.scalar(text("SELECT session_user"))
        if authenticated_role != expected_role:
            raise RuntimeError(f"database connection must authenticate as {expected_role}")
        yield session


def session_scope():
    """Open an API session authenticated as `jarvis_app`."""
    return _session_scope(settings.database_url, "jarvis_app")


def worker_session_scope():
    """Open a worker session; refuse the API URL or any unexpected DB role."""
    if not settings.worker_database_url:
        raise RuntimeError("WORKER_DATABASE_URL must configure the jarvis_worker role")
    return _session_scope(settings.worker_database_url, "jarvis_worker")


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
    """Set the legacy marker; RLS authorization uses the authenticated DB role."""
    await session.execute(text("SELECT set_config('app.worker', 'true', true)"))
