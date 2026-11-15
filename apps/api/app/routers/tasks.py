"""Task API. Every query filters on the authenticated user id.

Foreign and missing ids both return 404. Lease tokens and worker ids are not
accepted from clients.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.provider_factory import NotConfigured, get_model
from app.ai.supervisor import plan_request
from app.observe import record_usage
from app.integrations.requirements import missing_connection
from app.db import get_session
from app.jobs import (
    IdempotencyConflict,
    lookup_idempotency,
    request_hash,
    resolve_idempotency,
)
from app.limits import limit_writes
from app.models import User
from app.security import error, require_user
from app.tasks_service import create_task_from_plan

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


class TaskIn(BaseModel):
    instruction: str = Field(min_length=1, max_length=20_000)
    priority: int = Field(default=0, ge=-10, le=10)

    model_config = {"extra": "ignore"}


def _reject_client_owned_fields(body: dict) -> None:
    """Lease, owner, and status are server-owned. Clients cannot set them."""
    forbidden = {"worker_id", "lease_token", "lease_expires_at", "user_id", "status"}
    if forbidden & body.keys():
        raise HTTPException(400, error("invalid_request", "Server-owned fields are not accepted."))


@router.post("")
async def create_task(
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    limit_writes(user.id)
    raw = await request.json()
    if not isinstance(raw, dict):
        raise HTTPException(400, error("invalid_request", "Request body must be an object."))
    # Checked before validation so extra=ignore cannot hide a client-owned field.
    _reject_client_owned_fields(raw)
    try:
        body = TaskIn.model_validate(raw)
    except ValidationError:
        raise HTTPException(400, error("invalid_request", "Invalid task request."))
    digest = request_hash(body.instruction, body.priority)
    if idempotency_key:
        existing = await lookup_idempotency(session, user.id, idempotency_key)
        if existing is not None:
            previous_hash, task_id = existing
            if previous_hash != digest:
                raise HTTPException(
                    409, error("idempotency_conflict", "Key reused with a different request.")
                )
            return await _task_body(session, user.id, task_id, status_code=200)

    try:
        model = get_model()
    except NotConfigured:
        model = None
    plan, usage = await plan_request(model, body.instruction)
    usage_row = None
    if usage is not None and model is not None:
        usage_row = await record_usage(
            session,
            user_id=user.id,
            task_id=None,
            provider=model.name,
            model=model.model,
            usage=usage,
        )
    if not plan.requires_execution:
        raise HTTPException(422, error("not_executable", "The instruction did not name a runnable agent."))
    missing = await missing_connection(session, user.id, plan.agents)
    if missing:
        raise HTTPException(
            409,
            error("integration_required", f"Connect {missing.replace('_', ' ')} before this can run."),
        )

    # One transaction: task, steps, job (if any), priority, usage, and the key.
    # A unique-key conflict rolls all of them back. Approval plans insert no job.
    try:
        task_id = await create_task_from_plan(
            session,
            user.id,
            None,
            plan,
            enqueue=not plan.requires_approval,
            commit=False,
            priority=body.priority,
            idempotency_key=idempotency_key,
            request_hash=digest if idempotency_key else None,
        )
        if usage_row is not None:
            usage_row.task_id = task_id
        await session.commit()
    except IntegrityError:
        await session.rollback()
        if not idempotency_key:
            raise
        task_id = await _replay_after_conflict(session, user.id, idempotency_key, digest)
        return await _task_body(session, user.id, task_id, status_code=200)

    status_code = 202 if plan.requires_approval else 201
    return await _task_body(session, user.id, task_id, status_code=status_code)


@router.get("")
async def list_tasks(
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    limit: int = 20,
) -> dict:
    limit = min(max(limit, 1), 50)
    rows = (
        await session.execute(
            text(
                """
                SELECT id, title, status, created_at, updated_at
                FROM tasks
                WHERE user_id = :user_id
                ORDER BY created_at DESC, id DESC
                LIMIT :limit
                """
            ),
            {"user_id": user.id, "limit": limit},
        )
    ).mappings().all()
    return {"tasks": [_public_task(row) for row in rows]}


@router.get("/{task_id}")
async def get_task(
    task_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await _task_body(session, user.id, task_id, status_code=200)


@router.get("/{task_id}/jobs")
async def list_jobs(
    task_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _owned_task(session, user.id, task_id)
    rows = (
        await session.execute(
            text(
                """
                SELECT id, status, attempt_count, max_attempts, last_error,
                       available_at, started_at, completed_at, created_at
                FROM jobs
                WHERE task_id = :task_id AND user_id = :user_id
                ORDER BY created_at
                """
            ),
            {"task_id": task_id, "user_id": user.id},
        )
    ).mappings().all()
    return {"jobs": [dict(row) | {"id": str(row["id"])} for row in rows]}


@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    task = await _owned_task(session, user.id, task_id)
    if task["status"] in ("completed", "cancelled", "failed"):
        raise HTTPException(409, error("not_cancellable", "The task is already terminal."))
    updated = await session.execute(
        text(
            """
            UPDATE jobs
            SET status = 'cancelled', completed_at = now(), cancel_requested = false
            WHERE task_id = :task_id AND user_id = :user_id
              AND status IN ('queued', 'retry_wait')
            """
        ),
        {"task_id": task_id, "user_id": user.id},
    )
    if updated.rowcount:
        await _set_task_status(session, task_id, task["status"], "cancelled", "user", "cancelled")
        await session.commit()
        return await _task_body(session, user.id, task_id, status_code=200)
    running = await session.execute(
        text(
            """
            UPDATE jobs
            SET cancel_requested = true
            WHERE task_id = :task_id AND user_id = :user_id AND status = 'running'
            """
        ),
        {"task_id": task_id, "user_id": user.id},
    )
    await session.commit()
    if running.rowcount == 0:
        raise HTTPException(409, error("not_cancellable", "No active job to cancel."))
    return await _task_body(session, user.id, task_id, status_code=202)


@router.post("/{task_id}/retry")
async def retry_task(
    task_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    task = await _owned_task(session, user.id, task_id)
    if task["status"] != "failed":
        raise HTTPException(409, error("not_retryable", "Only a failed task can be retried."))
    agents = (
        await session.execute(
            text("SELECT agent_id FROM task_steps WHERE task_id = :id"),
            {"id": task_id},
        )
    ).scalars().all()
    missing = await missing_connection(session, user.id, list(agents))
    if missing:
        raise HTTPException(
            409,
            error("integration_required", f"Connect {missing.replace('_', ' ')} before this can run."),
        )
    from app.jobs import insert_job

    try:
        await insert_job(session, user_id=user.id, task_id=task_id)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, error("job_active", "An active job already exists for this task."))
    return await _task_body(session, user.id, task_id, status_code=202)


async def _replay_after_conflict(
    session: AsyncSession, user_id: uuid.UUID, key: str, digest: str
) -> uuid.UUID:
    """Read the key that won the unique constraint. Never inserts."""
    try:
        return await resolve_idempotency(session, user_id, key, digest)
    except IdempotencyConflict:
        raise HTTPException(
            409, error("idempotency_conflict", "Key reused with a different request.")
        )


async def _owned_task(session: AsyncSession, user_id: uuid.UUID, task_id: uuid.UUID):
    row = (
        await session.execute(
            text("SELECT * FROM tasks WHERE id = :id AND user_id = :user_id"),
            {"id": task_id, "user_id": user_id},
        )
    ).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, error("not_found", "Task not found."))
    return row


async def _task_body(
    session: AsyncSession, user_id: uuid.UUID, task_id: uuid.UUID, *, status_code: int
) -> dict:
    task = await _owned_task(session, user_id, task_id)
    steps = (
        await session.execute(
            text(
                """
                SELECT position, agent_id, action, risk, status, attempt
                FROM task_steps
                WHERE task_id = :id
                ORDER BY position
                """
            ),
            {"id": task_id},
        )
    ).mappings().all()
    job = (
        await session.execute(
            text(
                """
                SELECT id, status, attempt_count, last_error, cancel_requested, created_at, completed_at
                FROM jobs
                WHERE task_id = :id AND user_id = :user_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"id": task_id, "user_id": user_id},
        )
    ).mappings().one_or_none()
    reported = task["status"]
    if reported == "completed" and (job is None or job["status"] != "completed"):
        reported = "executing"
    body = {
        "id": str(task["id"]),
        "status": reported,
        "title": task["title"],
        "created_at": task["created_at"],
        "steps": [dict(step) for step in steps],
        "job": None
        if job is None
        else {
            "id": str(job["id"]),
            "status": job["status"],
            "attempt_count": job["attempt_count"],
            "last_error": job["last_error"],
            "cancel_requested": job["cancel_requested"],
        },
    }
    if status_code != 200:
        body["http_status"] = status_code
    return body


def _public_task(row) -> dict:
    return {
        "id": str(row["id"]),
        "title": row["title"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


async def _set_task_status(
    session: AsyncSession,
    task_id: uuid.UUID,
    current: str,
    target: str,
    actor: str,
    reason: str,
) -> None:
    from jarvis_shared.tasks import IllegalTransition, TaskStatus, apply_transition

    try:
        apply_transition(TaskStatus(current), TaskStatus(target))
    except IllegalTransition:
        raise HTTPException(409, error("illegal_transition", "The task cannot be cancelled from this state."))
    now = datetime.now(UTC)
    await session.execute(
        text("UPDATE tasks SET status = :status, updated_at = :now WHERE id = :id"),
        {"status": target, "now": now, "id": task_id},
    )
    await session.execute(
        text(
            """
            INSERT INTO task_events (id, task_id, from_status, to_status, reason, actor, created_at)
            VALUES (:id, :task_id, :from_status, :to_status, :reason, :actor, :now)
            """
        ),
        {
            "id": uuid.uuid4(),
            "task_id": task_id,
            "from_status": current,
            "to_status": target,
            "reason": reason,
            "actor": actor,
            "now": now,
        },
    )
