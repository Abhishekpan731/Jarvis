"""Task API. Every query filters on the authenticated user id.

Foreign and missing ids both return 404. Lease tokens and worker ids are not
accepted from clients.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.provider_factory import NotConfigured, get_model
from app.ai.supervisor import plan_request
from app.db import get_session, set_tenant_guc
from app.integrations.requirements import missing_connection
from app.jobs import (
    IdempotencyConflict,
    insert_job,
    lookup_idempotency,
    request_hash,
    resolve_idempotency,
)
from app.limits import limit_writes_durable
from app.models import User
from app.observe import record_usage
from app.security import error, require_user
from app.tasks_service import create_task_from_plan

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

_APPROVAL_TTL = timedelta(hours=24)
_PUBLIC_TASK_ERRORS = frozenset({"approval_expired", "approval_rejected"})


class TaskIn(BaseModel):
    instruction: str = Field(min_length=1, max_length=20_000)
    priority: int = Field(default=0, ge=-10, le=10)

    model_config = {"extra": "ignore"}


class ApprovalDecisionIn(BaseModel):
    """Hash of the exact planned actions shown to the task owner."""

    action_hash: str = Field(
        min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$"
    )


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
    """Create a task. The optional key preserves legacy non-idempotent clients.

    Without ``Idempotency-Key``, each request creates a separate task. New
    callers should send the key so retries replay safely.
    """
    await limit_writes_durable(user.id)
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
        if plan.requires_approval:
            action_hash = _approval_action_hash(
                [(position, step.agent, step.action) for position, step in enumerate(plan.steps)]
            )
            await session.execute(
                text(
                    """
                    UPDATE tasks
                    SET approval_expires_at = clock_timestamp() + interval '24 hours',
                        approval_action_hash = :action_hash
                    WHERE id = :task_id AND user_id = :user_id
                    """
                ),
                {
                    "action_hash": action_hash,
                    "task_id": task_id,
                    "user_id": user.id,
                }
            )
        if usage_row is not None:
            usage_row.task_id = task_id
        await session.commit()
        # set_config(..., true) is transaction-local: commit ends the
        # transaction it scoped, so every further RLS-guarded read on this
        # session must re-scope it first.
        await set_tenant_guc(session, user.id)
    except IntegrityError:
        await session.rollback()
        await set_tenant_guc(session, user.id)
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


@router.post("/{task_id}/approve")
async def approve_task(
    task_id: uuid.UUID,
    body: ApprovalDecisionIn,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await limit_writes_durable(user.id)
    return await _decide_approval(session, user, task_id, body.action_hash, "approved")


@router.post("/{task_id}/reject")
async def reject_task(
    task_id: uuid.UUID,
    body: ApprovalDecisionIn,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await limit_writes_durable(user.id)
    return await _decide_approval(session, user, task_id, body.action_hash, "rejected")


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
    await limit_writes_durable(user.id)
    task = await _owned_task(session, user.id, task_id, for_update=True)
    if task["status"] == "waiting_for_approval":
        task, _ = await _ensure_approval_metadata(session, user.id, task)
        if _approval_is_expired(task["approval_expires_at"], task["database_now"]):
            await _expire_approval(session, user.id, task_id)
            raise HTTPException(409, error("not_cancellable", "The approval has expired."))
        await _set_approval_status(session, user, task_id, "cancelled", "user", "cancelled")
        await session.commit()
        await set_tenant_guc(session, user.id)
        return await _task_body(session, user.id, task_id, status_code=200)
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
        await set_tenant_guc(session, user.id)
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
    await set_tenant_guc(session, user.id)
    if running.rowcount == 0:
        raise HTTPException(409, error("not_cancellable", "No active job to cancel."))
    return await _task_body(session, user.id, task_id, status_code=202)


@router.post("/{task_id}/retry")
async def retry_task(
    task_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await limit_writes_durable(user.id)
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
    try:
        await insert_job(session, user_id=user.id, task_id=task_id)
        await session.commit()
        await set_tenant_guc(session, user.id)
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


async def _owned_task(
    session: AsyncSession,
    user_id: uuid.UUID,
    task_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> dict:
    query = (
        "SELECT *, clock_timestamp() AS database_now "
        "FROM tasks WHERE id = :id AND user_id = :user_id"
    )
    if for_update:
        query += " FOR UPDATE"
    row = (
        await session.execute(text(query), {"id": task_id, "user_id": user_id})
    ).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, error("not_found", "Task not found."))
    return row


async def _task_body(
    session: AsyncSession, user_id: uuid.UUID, task_id: uuid.UUID, *, status_code: int
) -> dict:
    task = await _prepare_task_for_response(session, user_id, task_id)
    steps = (
        await session.execute(
            text(
                """
                SELECT position, agent_id, action, risk, status, attempt
                FROM task_steps WHERE task_id = :id ORDER BY position
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
                FROM jobs WHERE task_id = :id AND user_id = :user_id
                ORDER BY created_at DESC LIMIT 1
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
        "approval_action_hash": task["approval_action_hash"],
        "approval_expires_at": task["approval_expires_at"],
        "error": task["error"] if task["error"] in _PUBLIC_TASK_ERRORS else None,
        "steps": [dict(step) for step in steps],
        "job": _job_payload(job),
    }
    if status_code != 200:
        body["http_status"] = status_code
    return body


def _job_payload(job) -> dict | None:
    """Convert a database job row to its public response shape."""
    if job is None:
        return None
    return {
        "id": str(job["id"]),
        "status": job["status"],
        "attempt_count": job["attempt_count"],
        "last_error": job["last_error"],
        "cancel_requested": job["cancel_requested"],
    }


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


def _approval_action_hash(actions: list[tuple[int, str, str]]) -> str:
    """Hash ordered execution steps, including their agent and exact action."""
    body = json.dumps(actions, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _approval_is_expired(expires_at: datetime | None, now: datetime) -> bool:
    """Treat the exact expiry instant as expired."""
    return expires_at is not None and expires_at <= now


async def _approval_hash_for_task(session: AsyncSession, task_id: uuid.UUID) -> str:
    """Recompute the hash from persisted, ordered task steps."""
    rows = (
        await session.execute(
            text(
                """
                SELECT position, agent_id, action FROM task_steps
                WHERE task_id = :task_id ORDER BY position
                """
            ),
            {"task_id": task_id},
        )
    ).mappings().all()
    actions = [(row["position"], row["agent_id"], row["action"]) for row in rows]
    return _approval_action_hash(actions)


async def _ensure_approval_metadata(
    session: AsyncSession, user_id: uuid.UUID, task
) -> tuple[dict, bool]:
    """Backfill legacy waiting tasks without changing an existing hash."""
    current_hash = await _approval_hash_for_task(session, task["id"])
    saved_hash = task["approval_action_hash"]
    if saved_hash is not None and saved_hash != current_hash:
        raise HTTPException(409, error("approval_plan_changed", "The planned actions changed."))
    expires_at = task["approval_expires_at"] or task["created_at"] + _APPROVAL_TTL
    changed = saved_hash is None or task["approval_expires_at"] is None
    if changed:
        await session.execute(
            text(
                """
                UPDATE tasks
                SET approval_action_hash = :action_hash, approval_expires_at = :expires_at
                WHERE id = :task_id AND user_id = :user_id AND status = 'waiting_for_approval'
                """
            ),
            {
                "action_hash": current_hash,
                "expires_at": expires_at,
                "task_id": task["id"],
                "user_id": user_id,
            },
        )
        task = await _owned_task(session, user_id, task["id"], for_update=True)
    return task, changed


async def _prepare_task_for_response(
    session: AsyncSession, user_id: uuid.UUID, task_id: uuid.UUID
) -> dict:
    """Fill missing metadata and terminalize overdue approvals."""
    task = await _owned_task(session, user_id, task_id, for_update=True)
    if task["status"] != "waiting_for_approval":
        return task
    task, changed = await _ensure_approval_metadata(session, user_id, task)
    if changed:
        await session.commit()
        await set_tenant_guc(session, user_id)
        task = await _owned_task(session, user_id, task_id, for_update=True)
    if task["status"] == "waiting_for_approval" and _approval_is_expired(
        task["approval_expires_at"], task["database_now"]
    ):
        await _expire_approval(session, user_id, task_id)
        task = await _owned_task(session, user_id, task_id)
    return task


async def _expire_approval(
    session: AsyncSession, user_id: uuid.UUID, task_id: uuid.UUID
) -> None:
    """Make an expired approval terminal exactly once."""
    from jarvis_shared.tasks import IllegalTransition, TaskStatus, apply_transition

    try:
        apply_transition(TaskStatus.WAITING_FOR_APPROVAL, TaskStatus.FAILED)
    except IllegalTransition as exc:
        raise HTTPException(
            409, error("illegal_transition", "The expiry transition is invalid.")
        ) from exc
    updated = await session.execute(
        text(
            """
            UPDATE tasks
            SET status = 'failed', error = 'approval_expired', updated_at = clock_timestamp()
            WHERE id = :task_id AND user_id = :user_id
              AND status = 'waiting_for_approval' AND approval_expires_at <= clock_timestamp()
            RETURNING id
            """
        ),
        {"task_id": task_id, "user_id": user_id},
    )
    if updated.scalar_one_or_none() is None:
        return
    await _record_approval_event(
        session, task_id, "failed", "approval_expired", "system"
    )
    await session.commit()
    await set_tenant_guc(session, user_id)


async def _approval_decision(session: AsyncSession, task_id: uuid.UUID) -> str | None:
    """Return the first recorded decision by the task owner."""
    reason = (
        await session.execute(
            text(
                """
                SELECT reason FROM task_events
                WHERE task_id = :task_id AND actor = 'owner'
                  AND reason IN ('approval_approved', 'approval_rejected')
                ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"task_id": task_id},
        )
    ).scalar_one_or_none()
    return reason.removeprefix("approval_") if reason else None


async def _decide_approval(
    session: AsyncSession, user: User, task_id: uuid.UUID, action_hash: str, decision: str
) -> dict:
    """Apply one owner decision, replaying identical decisions safely."""
    task = await _owned_task(session, user.id, task_id, for_update=True)
    if task["error"] == "approval_expired":
        raise HTTPException(410, error("approval_expired", "The approval window has expired."))
    if task["status"] == "waiting_for_approval":
        task, _ = await _ensure_approval_metadata(session, user.id, task)
        if _approval_is_expired(task["approval_expires_at"], task["database_now"]):
            await _expire_approval(session, user.id, task_id)
            raise HTTPException(410, error("approval_expired", "The approval window has expired."))
    current_hash = await _approval_hash_for_task(session, task_id)
    if task["approval_action_hash"] != current_hash or action_hash != current_hash:
        raise HTTPException(409, error("approval_hash_mismatch", "The planned actions do not match."))
    prior = await _approval_decision(session, task_id)
    if prior is not None:
        if prior != decision:
            raise HTTPException(409, error("approval_decided", "A different decision was already recorded."))
        return await _task_body(session, user.id, task_id, status_code=200)
    if task["status"] != "waiting_for_approval":
        raise HTTPException(409, error("not_awaiting_approval", "The task is not awaiting approval."))
    if decision == "approved":
        await _approve_task(session, user, task_id)
    else:
        await _reject_task(session, user, task_id)
    await session.commit()
    await set_tenant_guc(session, user.id)
    return await _task_body(session, user.id, task_id, status_code=200)


async def _approve_task(session: AsyncSession, user: User, task_id: uuid.UUID) -> None:
    """Queue one validated task after its owner approves it."""
    agents = (
        await session.execute(
            text("SELECT agent_id FROM task_steps WHERE task_id = :task_id ORDER BY position"),
            {"task_id": task_id},
        )
    ).scalars().all()
    if not agents:
        raise HTTPException(409, error("empty_plan", "The task has no executable actions."))
    missing = await missing_connection(session, user.id, list(agents))
    if missing:
        raise HTTPException(
            409, error("integration_required", f"Connect {missing.replace('_', ' ')} before this can run."),
        )
    await _set_approval_status(session, user, task_id, "executing", "owner", "approval_approved")
    await insert_job(session, user_id=user.id, task_id=task_id)


async def _reject_task(session: AsyncSession, user: User, task_id: uuid.UUID) -> None:
    """Reject a task through the legal cancellation transition."""
    await _set_approval_status(
        session, user, task_id, "cancelled", "owner", "approval_rejected"
    )


async def _set_approval_status(
    session: AsyncSession,
    user: User,
    task_id: uuid.UUID,
    target: str,
    actor: str,
    reason: str,
) -> None:
    """Change a waiting task only if its approval deadline has not passed."""
    from jarvis_shared.tasks import IllegalTransition, TaskStatus, apply_transition

    try:
        apply_transition(TaskStatus.WAITING_FOR_APPROVAL, TaskStatus(target))
    except IllegalTransition:
        raise HTTPException(409, error("illegal_transition", "The approval transition is invalid."))
    updated = await session.execute(
        text(
            """
            UPDATE tasks
            SET status = :target,
                error = CASE WHEN :reason = 'approval_rejected' THEN 'approval_rejected'
                             WHEN :reason = 'cancelled' THEN 'cancelled' ELSE error END,
                updated_at = clock_timestamp()
            WHERE id = :task_id AND user_id = :user_id
              AND status = 'waiting_for_approval'
              AND approval_expires_at > clock_timestamp()
            RETURNING id
            """
        ),
        {
            "target": target,
            "reason": reason,
            "task_id": task_id,
            "user_id": user.id,
        },
    )
    if updated.scalar_one_or_none() is None:
        await _expire_approval(session, user.id, task_id)
        raise HTTPException(410, error("approval_expired", "The approval window has expired."))
    await _record_approval_event(session, task_id, target, reason, actor)


async def _record_approval_event(
    session: AsyncSession, task_id: uuid.UUID, target: str, reason: str, actor: str
) -> None:
    """Record one approval transition in the task's audit trail."""
    await session.execute(
        text(
            """
            INSERT INTO task_events (id, task_id, from_status, to_status, reason, actor, created_at)
            VALUES (:id, :task_id, 'waiting_for_approval', :target, :reason, :actor, clock_timestamp())
            """
        ),
        {
            "id": uuid.uuid4(),
            "task_id": task_id,
            "target": target,
            "reason": reason,
            "actor": actor,
        },
    )
