"""Counts an operator can read. No message text.

A trace is one line: ids, a model name, and token counts. The prompt and the
reply stay in the conversation tables, which this module does not query.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.schemas import Usage
from app.ai.usage import estimate_cost_usd
from app.db import set_worker_guc
from app.models import ModelUsage

log = logging.getLogger("jarvis.observe")


def trace(event: str, **fields: str | int) -> None:
    """Log an event that contains no user content."""
    safe = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
    log.info("%s %s", event, safe)


async def record_usage(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    task_id: uuid.UUID | None,
    provider: str,
    model: str,
    usage: Usage,
) -> ModelUsage:
    """Store token counts. The caller does not pass the prompt.

    The row is returned so a caller that creates the task afterwards can set
    task_id before committing. Nothing here reads the prompt.
    """
    row = ModelUsage(
        id=uuid.uuid4(),
        user_id=user_id,
        task_id=task_id,
        provider=provider[:40],
        model=model[:80],
        input_tokens=max(usage.input_tokens, 0),
        output_tokens=max(usage.output_tokens, 0),
        estimated_cost_usd=estimate_cost_usd(model, usage.input_tokens, usage.output_tokens),
    )
    session.add(row)
    trace(
        "model_usage",
        user_id=str(user_id),
        task_id=str(task_id or ""),
        provider=provider,
        model=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )
    return row


async def queue_depth(session: AsyncSession) -> dict[str, int]:
    """Count jobs by status. The query selects no payload and no instruction.

    Deliberately narrow admin bypass: this aggregate must see every tenant's
    jobs, so it sets the worker escape-hatch GUC rather than a tenant GUC.
    """
    await set_worker_guc(session)
    rows = (
        await session.execute(text("SELECT status, count(*) FROM jobs GROUP BY status"))
    ).all()
    counts = {status: int(count) for status, count in rows}
    return {
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "retry_wait": counts.get("retry_wait", 0),
        "failed": counts.get("failed", 0),
    }


async def usage_totals(session: AsyncSession) -> dict[str, int]:
    row = (
        await session.execute(
            text(
                """
                SELECT coalesce(sum(input_tokens), 0),
                       coalesce(sum(output_tokens), 0),
                       count(*),
                       coalesce(sum(estimated_cost_usd), 0)
                FROM model_usage
                """
            )
        )
    ).one()
    return {
        "input_tokens": int(row[0]),
        "output_tokens": int(row[1]),
        "calls": int(row[2]),
        "estimated_cost_usd": float(row[3]),
    }
