"""In-process write limit. A restart clears it.

This is not a distributed limiter. It stops one process from accepting an
unbounded burst of task and command writes from one account. Login throttling
stays in the auth router and is counted separately.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict

from fastapi import HTTPException

from app.security import error

_HITS: dict[str, list[float]] = defaultdict(list)
_WINDOW_SECONDS = 60
_MAX_WRITES = 30


def reset() -> None:
    """Clear counters. Tests use this so one case cannot exhaust the next."""
    _HITS.clear()


def limit_writes(user_id: uuid.UUID, *, limit: int = _MAX_WRITES) -> None:
    """Raise 429 once this account has used its write budget for the window."""
    now = time.monotonic()
    key = str(user_id)
    recent = [stamp for stamp in _HITS[key] if now - stamp < _WINDOW_SECONDS]
    if len(recent) >= limit:
        _HITS[key] = recent
        raise HTTPException(429, error("rate_limited", "Too many requests. Try again later."))
    recent.append(now)
    _HITS[key] = recent
