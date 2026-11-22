"""Write throttles: a durable database limiter plus legacy process-local support.

The old synchronous helper remains for out-of-scope callers. New async routes
should use ``limit_writes_durable`` so the cap survives processes and restarts.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections import defaultdict

from fastapi import HTTPException
from sqlalchemy import text

from app.db import session_scope
from app.security import error

_HITS: dict[str, list[float]] = defaultdict(list)
_WINDOW_SECONDS = 60
_MAX_WRITES = 30


def reset() -> None:
    """Clear counters. Tests use this so one case cannot exhaust the next."""
    _HITS.clear()


def limit_writes(user_id: uuid.UUID, *, limit: int = _MAX_WRITES) -> None:
    """Legacy in-process throttle; async routes should use the durable limiter."""
    now = time.monotonic()
    key = str(user_id)
    recent = [stamp for stamp in _HITS[key] if now - stamp < _WINDOW_SECONDS]
    if len(recent) >= limit:
        _HITS[key] = recent
        raise HTTPException(429, error("rate_limited", "Too many requests. Try again later."))
    recent.append(now)
    _HITS[key] = recent


class DurableWriteLimiter:
    """Atomically enforce account write budgets in PostgreSQL."""

    async def limit(self, user_id: uuid.UUID, *, limit: int = _MAX_WRITES) -> None:
        """Consume one fixed-minute request slot or raise HTTP 429."""
        if limit < 1:
            raise ValueError("limit must be positive")
        bucket_key = "writes:" + hashlib.sha256(str(user_id).encode()).hexdigest()
        async with session_scope() as session:
            await session.execute(
                text(
                    """
                    DELETE FROM rate_limit_windows
                    WHERE bucket_key = :bucket_key
                      AND window_start < (
                          date_trunc('minute', clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                      )
                    """
                ),
                {"bucket_key": bucket_key},
            )
            result = await session.execute(
                text(
                    """
                    INSERT INTO rate_limit_windows (bucket_key, window_start, request_count)
                    VALUES (
                        :bucket_key,
                        (date_trunc('minute', clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'),
                        1
                    )
                    ON CONFLICT (bucket_key, window_start) DO UPDATE
                    SET request_count = rate_limit_windows.request_count + 1
                    WHERE rate_limit_windows.request_count < :limit
                    RETURNING request_count
                    """
                ),
                {"bucket_key": bucket_key, "limit": limit},
            )
            if result.scalar_one_or_none() is None:
                await session.rollback()
                raise HTTPException(
                    429, error("rate_limited", "Too many requests. Try again later.")
                )
            await session.commit()



async def limit_writes_durable(
    user_id: uuid.UUID, *, limit: int = _MAX_WRITES
) -> None:
    """Enforce the shared PostgreSQL write limit for one account."""
    await _DURABLE_LIMITER.limit(user_id, limit=limit)


_DURABLE_LIMITER = DurableWriteLimiter()
