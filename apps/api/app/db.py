"""Database engine and session management.

The engine is built per event loop. asyncpg connections cannot move between
loops, and the test suite creates a new loop per test.
"""

import asyncio
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

_engines: dict[int, tuple] = {}


class Base(DeclarativeBase):
    pass


def _factory() -> async_sessionmaker:
    loop_id = id(asyncio.get_running_loop())
    if loop_id not in _engines:
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        _engines[loop_id] = (engine, async_sessionmaker(engine, expire_on_commit=False))
    return _engines[loop_id][1]


def session_scope() -> AsyncSession:
    """Open a session. Use as `async with session_scope() as session`."""
    return _factory()()


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session
