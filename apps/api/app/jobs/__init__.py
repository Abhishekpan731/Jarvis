"""PostgreSQL job queue.

Claim, heartbeat, and result writes are short transactions. The step runner
is never called while a transaction is open. Lease tokens are minted here,
never accepted from an HTTP client.

RLS is a second barrier, not a replacement for the explicit WHERE user_id
filters below. Cross-tenant queue access requires the authenticated
`jarvis_worker` database login; the transaction-local app.user_id still scopes
tenant-owned task and integration rows.
"""

from __future__ import annotations

import hashlib
import json
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import set_tenant_guc, set_worker_guc

JOB_TYPE = "task_execution"
ACTIVE = ("queued", "running", "retry_wait")

PERMANENT_ERRORS = frozenset(
    {
        "approval_required",
        "unknown_agent",
        "empty_plan",
        "illegal_transition",
        "validation_error",
        "not_runnable",
        "integration_required",
        "integration_not_configured",
        "integration_token_rejected",
    }
)


async def _enter_worker_scope(session: AsyncSession, job_id: uuid.UUID) -> None:
    """Scope subsequent task work to the job owner after the worker role reads it."""
    await set_worker_guc(session)
    owner = (
        await session.execute(text("SELECT user_id FROM jobs WHERE id = :id"), {"id": job_id})
    ).scalar_one_or_none()
    if owner is not None:
        await set_tenant_guc(session, owner)


class IdempotencyConflict(Exception):
    """Same idempotency key was reused with a different request body."""


class LeaseLost(Exception):
    """The worker no longer owns the job. It must stop writing."""


def request_hash(instruction: str, priority: int = 0) -> str:
    body = json.dumps(
        {"instruction": instruction, "job_type": JOB_TYPE, "priority": priority},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def backoff_seconds(attempt_count: int) -> float:
    """Full jitter up to the exponential cap. attempt_count is 1-based after a claim."""
    cap = min(
        settings.job_backoff_max_seconds,
        settings.job_backoff_base_seconds * (2 ** max(attempt_count - 1, 0)),
    )
    return random.uniform(0, cap) if cap > 0 else 0


def job_payload(task_id: uuid.UUID) -> dict[str, str]:
    """References only. Extra keys are ignored by the worker."""
    return {"task_id": str(task_id), "job_type": JOB_TYPE}


async def insert_job(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    task_id: uuid.UUID,
    priority: int = 0,
    max_attempts: int | None = None,
) -> uuid.UUID:
    """Insert a queued job. Caller owns the transaction."""
    job_id = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO jobs (
                id, user_id, task_id, job_type, payload, status, priority,
                attempt_count, max_attempts, available_at, cancel_requested, created_at
            ) VALUES (
                :id, :user_id, :task_id, :job_type, CAST(:payload AS jsonb), 'queued', :priority,
                0, :max_attempts, now(), false, now()
            )
            """
        ),
        {
            "id": job_id,
            "user_id": user_id,
            "task_id": task_id,
            "job_type": JOB_TYPE,
            "payload": json.dumps(job_payload(task_id)),
            "priority": priority,
            "max_attempts": max_attempts if max_attempts is not None else settings.job_max_attempts,
        },
    )
    return job_id


async def lookup_idempotency(
    session: AsyncSession, user_id: uuid.UUID, key: str
) -> tuple[str, uuid.UUID] | None:
    row = (
        await session.execute(
            text(
                """
                SELECT request_hash, task_id
                FROM idempotency_keys
                WHERE user_id = :user_id AND key = :key
                """
            ),
            {"user_id": user_id, "key": key},
        )
    ).one_or_none()
    if row is None:
        return None
    return row.request_hash, row.task_id


async def store_idempotency(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    key: str,
    request_hash: str,
    task_id: uuid.UUID,
) -> None:
    """Insert a key row. Caller owns the transaction and the unique-violation rollback.

    Does not commit and does not catch IntegrityError. A unique violation must
    roll back the task and job that share this transaction, not only this row.
    """
    await session.execute(
        text(
            """
            INSERT INTO idempotency_keys (user_id, key, request_hash, task_id, created_at)
            VALUES (:user_id, :key, :request_hash, :task_id, now())
            """
        ),
        {
            "user_id": user_id,
            "key": key,
            "request_hash": request_hash,
            "task_id": task_id,
        },
    )


async def resolve_idempotency(
    session: AsyncSession, user_id: uuid.UUID, key: str, digest: str
) -> uuid.UUID:
    """Re-read a key after a rolled-back insert.

    Same hash returns the original task. A different hash, or a key that is
    not yet visible, raises IdempotencyConflict.
    """
    existing = await lookup_idempotency(session, user_id, key)
    if existing is None or existing[0] != digest:
        raise IdempotencyConflict
    return existing[1]


_CLAIM = text(
    """
    WITH picked AS MATERIALIZED (
        SELECT id, user_id
        FROM jobs
        WHERE status = 'queued'
          AND available_at <= now()
          AND cancel_requested = false
        ORDER BY priority DESC, created_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    ),
    locked_user AS MATERIALIZED (
        SELECT j.id, j.status
        FROM jobs j
        JOIN picked p ON j.user_id = p.user_id
        WHERE j.status IN ('queued', 'running', 'retry_wait')
        ORDER BY j.id
        FOR UPDATE
    ),
    running_n AS (
        SELECT count(*) AS n
        FROM locked_user
        WHERE status = 'running'
    )
    UPDATE jobs
    SET status = 'running',
        locked_at = now(),
        lease_expires_at = now() + make_interval(secs => :lease_seconds),
        worker_id = :worker_id,
        lease_token = :new_token,
        heartbeat_at = now(),
        started_at = COALESCE(jobs.started_at, now()),
        attempt_count = jobs.attempt_count + 1
    WHERE id = (SELECT id FROM picked)
      AND status = 'queued'
      AND cancel_requested = false
      AND (SELECT n FROM running_n) < :per_user_max
      AND (SELECT count(*) FROM locked_user) >= 1
    RETURNING id, user_id, task_id, lease_token, attempt_count, max_attempts
    """
)


async def claim_one(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int | None = None,
    per_user_max: int | None = None,
) -> dict[str, Any] | None:
    """Claim one queued job and commit ownership. Returns None if nothing was claimed."""
    await set_worker_guc(session)
    token = uuid.uuid4()
    lease = lease_seconds if lease_seconds is not None else settings.worker_lease_seconds
    result = await session.execute(
        _CLAIM,
        {
            "lease_seconds": lease,
            "worker_id": worker_id,
            "new_token": token,
            "per_user_max": per_user_max
            if per_user_max is not None
            else settings.worker_per_user_max_running,
        },
    )
    row = result.mappings().one_or_none()
    if row is None:
        await session.rollback()
        return None
    await session.commit()
    return dict(row)


async def heartbeat(
    session: AsyncSession, job_id: uuid.UUID, token: uuid.UUID, lease_seconds: int | None = None
) -> bool:
    await _enter_worker_scope(session, job_id)
    result = await session.execute(
        text(
            """
            UPDATE jobs
            SET heartbeat_at = now(),
                lease_expires_at = now() + make_interval(secs => :lease_seconds)
            WHERE id = :id
              AND lease_token = :token
              AND status = 'running'
              AND lease_expires_at > now()
              AND cancel_requested = false
            """
        ),
        {
            "id": job_id,
            "token": token,
            "lease_seconds": lease_seconds
            if lease_seconds is not None
            else settings.worker_lease_seconds,
        },
    )
    await session.commit()
    return result.rowcount == 1


async def cancel_if_requested(
    session: AsyncSession, job_id: uuid.UUID, token: uuid.UUID, task_id: uuid.UUID
) -> bool:
    """Cancel a running job under the fence when cancel was requested.

    Matches id, lease_token, status running, and an unexpired lease. Rowcount 0
    means the lease was lost: stop, and do not write the task. A task that is
    already terminal is left as it is.
    """
    await _enter_worker_scope(session, job_id)
    updated = await session.execute(
        text(
            """
            UPDATE jobs
            SET status = 'cancelled',
                completed_at = now(),
                last_error = 'cancelled',
                worker_id = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                locked_at = NULL,
                cancel_requested = false
            WHERE id = :id
              AND lease_token = :token
              AND status = 'running'
              AND lease_expires_at > now()
              AND cancel_requested = true
            """
        ),
        {"id": job_id, "token": token},
    )
    if updated.rowcount != 1:
        await session.rollback()
        return False
    row = (
        await session.execute(
            text("SELECT status FROM tasks WHERE id = :id FOR UPDATE"),
            {"id": task_id},
        )
    ).one_or_none()
    if row is not None and row.status not in ("completed", "cancelled", "failed"):
        await _transition_task(session, task_id, "cancelled", "worker", "cancel requested")
    await session.commit()
    return True


async def still_owner(session: AsyncSession, job_id: uuid.UUID, token: uuid.UUID) -> bool:
    await _enter_worker_scope(session, job_id)
    row = (
        await session.execute(
            text(
                """
                SELECT 1 FROM jobs
                WHERE id = :id
                  AND lease_token = :token
                  AND status = 'running'
                  AND lease_expires_at > now()
                """
            ),
            {"id": job_id, "token": token},
        )
    ).one_or_none()
    return row is not None


async def fenced_execute(
    session: AsyncSession, statement: str, params: dict[str, Any]
) -> int:
    """Run one fence-checked statement and commit. Returns rowcount."""
    result = await session.execute(text(statement), params)
    await session.commit()
    return result.rowcount or 0


async def reap_expired(
    session: AsyncSession, *, limit: int = 10, only_id: uuid.UUID | None = None
) -> int:
    """Recover expired running jobs. Each job is its own transaction."""
    await set_worker_guc(session)
    ids = (
        await session.execute(
            text(
                """
                SELECT id FROM jobs
                WHERE status = 'running' AND lease_expires_at <= now()
                  AND (CAST(:only_id AS uuid) IS NULL OR id = CAST(:only_id AS uuid))
                ORDER BY id
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
                """
            ),
            {"limit": limit, "only_id": only_id},
        )
    ).scalars().all()
    await session.rollback()
    recovered = 0
    for job_id in ids:
        async with session.begin():
            await set_worker_guc(session)
            row = (
                await session.execute(
                    text(
                        """
                        SELECT id, task_id, user_id, attempt_count, max_attempts, cancel_requested
                        FROM jobs
                        WHERE id = :id AND status = 'running' AND lease_expires_at <= now()
                        FOR UPDATE SKIP LOCKED
                        """
                    ),
                    {"id": job_id},
                )
            ).mappings().one_or_none()
            if row is None:
                continue
            await set_tenant_guc(session, row["user_id"])
            if row["cancel_requested"]:
                await _terminal_job(session, row["id"], row["task_id"], "cancelled", "cancelled")
            elif row["attempt_count"] >= row["max_attempts"]:
                await _terminal_job(session, row["id"], row["task_id"], "failed", "lease_expired")
            else:
                delay = backoff_seconds(row["attempt_count"])
                await session.execute(
                    text(
                        """
                        UPDATE jobs
                        SET status = 'retry_wait',
                            available_at = now() + make_interval(secs => :delay),
                            worker_id = NULL,
                            lease_token = NULL,
                            lease_expires_at = NULL,
                            locked_at = NULL,
                            last_error = 'lease_expired'
                        WHERE id = :id AND status = 'running'
                        """
                    ),
                    {"id": row["id"], "delay": delay},
                )
            recovered += 1
    return recovered


async def promote_retry_wait(session: AsyncSession, *, limit: int = 10) -> int:
    await set_worker_guc(session)
    result = await session.execute(
        text(
            """
            WITH picked AS MATERIALIZED (
                SELECT id FROM jobs
                WHERE status = 'retry_wait'
                  AND cancel_requested = false
                  AND available_at <= now()
                ORDER BY id
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
            )
            UPDATE jobs
            SET status = 'queued'
            WHERE id IN (SELECT id FROM picked)
              AND status = 'retry_wait'
              AND cancel_requested = false
            """
        ),
        {"limit": limit},
    )
    await session.commit()
    return result.rowcount or 0


async def fail_job(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    token: uuid.UUID,
    task_id: uuid.UUID,
    error_code: str,
    attempt_count: int,
    max_attempts: int,
) -> str:
    """Move a fenced job to retry_wait or failed. Returns the new job status."""
    await _enter_worker_scope(session, job_id)
    permanent = error_code in PERMANENT_ERRORS or attempt_count >= max_attempts
    if permanent:
        updated = await session.execute(
            text(
                """
                UPDATE jobs
                SET status = 'failed',
                    completed_at = now(),
                    last_error = :error_code,
                    worker_id = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL
                WHERE id = :id
                  AND lease_token = :token
                  AND status = 'running'
                """
            ),
            {"id": job_id, "token": token, "error_code": error_code},
        )
        if updated.rowcount != 1:
            await session.rollback()
            raise LeaseLost
        await _fail_task_if_legal(session, task_id, error_code)
        await session.commit()
        return "failed"

    delay = backoff_seconds(attempt_count)
    updated = await session.execute(
        text(
            """
            UPDATE jobs
            SET status = 'retry_wait',
                available_at = now() + make_interval(secs => :delay),
                last_error = :error_code,
                worker_id = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                locked_at = NULL
            WHERE id = :id
              AND lease_token = :token
              AND status = 'running'
            """
        ),
        {"id": job_id, "token": token, "error_code": error_code, "delay": delay},
    )
    if updated.rowcount != 1:
        await session.rollback()
        raise LeaseLost
    await session.commit()
    return "retry_wait"


async def complete_job(session: AsyncSession, job_id: uuid.UUID, token: uuid.UUID) -> None:
    await _enter_worker_scope(session, job_id)
    updated = await session.execute(
        text(
            """
            UPDATE jobs
            SET status = 'completed',
                completed_at = now(),
                worker_id = NULL,
                lease_token = NULL,
                lease_expires_at = NULL
            WHERE id = :id
              AND lease_token = :token
              AND status = 'running'
              AND lease_expires_at > now()
            """
        ),
        {"id": job_id, "token": token},
    )
    if updated.rowcount != 1:
        await session.rollback()
        raise LeaseLost
    await session.commit()


async def _terminal_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    task_id: uuid.UUID,
    job_status: str,
    error_code: str,
) -> None:
    await session.execute(
        text(
            """
            UPDATE jobs
            SET status = :status,
                completed_at = now(),
                last_error = :error_code,
                worker_id = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                locked_at = NULL,
                cancel_requested = false
            WHERE id = :id
            """
        ),
        {"id": job_id, "status": job_status, "error_code": error_code},
    )
    if job_status == "cancelled":
        await _transition_task(session, task_id, "cancelled", "worker", "cancel requested")
    else:
        await _fail_task_if_legal(session, task_id, error_code)


async def _fail_task_if_legal(session: AsyncSession, task_id: uuid.UUID, error_code: str) -> None:
    row = (
        await session.execute(
            text("SELECT status FROM tasks WHERE id = :id FOR UPDATE"),
            {"id": task_id},
        )
    ).one()
    if row.status in ("created", "understanding", "planning", "executing", "verifying"):
        if row.status == "created":
            await _transition_task(session, task_id, "understanding", "worker", error_code)
            await _transition_task(session, task_id, "planning", "worker", error_code)
            await _transition_task(session, task_id, "executing", "worker", error_code)
        elif row.status == "understanding":
            await _transition_task(session, task_id, "planning", "worker", error_code)
            await _transition_task(session, task_id, "executing", "worker", error_code)
        elif row.status == "planning":
            await _transition_task(session, task_id, "executing", "worker", error_code)
        await _transition_task(session, task_id, "failed", "worker", error_code)
        await session.execute(
            text("UPDATE tasks SET error = :error, updated_at = now() WHERE id = :id"),
            {"id": task_id, "error": error_code},
        )


async def _transition_task(
    session: AsyncSession, task_id: uuid.UUID, target: str, actor: str, reason: str
) -> None:
    from jarvis_shared.tasks import TaskStatus, apply_transition

    row = (
        await session.execute(
            text("SELECT status FROM tasks WHERE id = :id FOR UPDATE"),
            {"id": task_id},
        )
    ).one()
    current = TaskStatus(row.status)
    if current.value == target:
        return
    apply_transition(current, TaskStatus(target))
    await session.execute(
        text("UPDATE tasks SET status = :status, updated_at = now() WHERE id = :id"),
        {"id": task_id, "status": target},
    )
    await session.execute(
        text(
            """
            INSERT INTO task_events (id, task_id, from_status, to_status, reason, actor, created_at)
            VALUES (:id, :task_id, :from_status, :to_status, :reason, :actor, now())
            """
        ),
        {
            "id": uuid.uuid4(),
            "task_id": task_id,
            "from_status": current.value,
            "to_status": target,
            "reason": reason[:200],
            "actor": actor,
        },
    )


def utcnow() -> datetime:
    return datetime.now(UTC)


def lease_deadline(seconds: int | None = None) -> datetime:
    return utcnow() + timedelta(seconds=seconds or settings.worker_lease_seconds)
