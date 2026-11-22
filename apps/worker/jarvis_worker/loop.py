"""Claim loop. One session per database operation. The runner gets none."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import uuid
from datetime import UTC, datetime

from app.briefing import run_due
from app.config import settings
from app.db import set_tenant_guc, set_worker_guc, worker_session_scope
from app.execution import LeaseLost, execute_task_durable
from app.integrations.runner import IntegrationStepError, run_step
from app.jobs import (
    cancel_if_requested,
    claim_one,
    complete_job,
    fail_job,
    heartbeat,
    promote_retry_wait,
    reap_expired,
)
from app.models import Task, TaskStep
from sqlalchemy import text

from jarvis_worker.health import start_health

log = logging.getLogger("jarvis.worker")


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"


def _log(msg: str, **fields) -> None:
    payload = {"ts": datetime.now(UTC).isoformat(), "level": "info", "msg": msg, **fields}
    log.info(json.dumps(payload, default=str))


class Runner:
    def __init__(self, worker: str) -> None:
        self.worker = worker
        self.shutdown = asyncio.Event()
        self.inflight = 0
        self.ready = False

    async def run(self) -> None:
        health = await start_health(self)
        _log("worker_started", worker_id=self.worker)
        try:
            while not self.shutdown.is_set():
                async with worker_session_scope() as session:
                    await reap_expired(session)
                    await promote_retry_wait(session)
                    try:
                        await set_worker_guc(session)
                        await run_due(session)
                        await session.commit()
                    except Exception:  # noqa: BLE001 - briefing is best-effort; keep job claiming alive.
                        await session.rollback()
                        _log("briefing_failed", worker_id=self.worker, error_code="briefing_failed")
                self.ready = True
                free = settings.worker_concurrency - self.inflight
                claimed = False
                for _ in range(max(free, 0)):
                    if self.shutdown.is_set():
                        break
                    async with worker_session_scope() as session:
                        job = await claim_one(session, worker_id=self.worker)
                    if job is None:
                        break
                    claimed = True
                    self.inflight += 1
                    asyncio.create_task(self._guard(job))
                if not claimed:
                    try:
                        await asyncio.wait_for(
                            self.shutdown.wait(), timeout=settings.worker_poll_interval_seconds
                        )
                    except TimeoutError:
                        pass
            deadline = asyncio.get_running_loop().time() + settings.worker_shutdown_grace_seconds
            while self.inflight and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.1)
        finally:
            self.ready = False
            health.close()
            _log("worker_stopped", worker_id=self.worker, inflight=self.inflight)

    async def _guard(self, job: dict) -> None:
        try:
            await self._run_job(job)
        except Exception:  # noqa: BLE001 - isolate ordinary per-job failures from the worker loop.
            _log(
                "job_unhandled",
                worker_id=self.worker,
                job_id=str(job.get("id")),
                error_code="unhandled",
            )
        finally:
            self.inflight -= 1

    async def _run_job(self, job: dict) -> None:
        job_id = job["id"]
        token = job["lease_token"]
        task_id = job["task_id"]
        user_id = job["user_id"]
        _log("job_claimed", worker_id=self.worker, job_id=str(job_id), task_id=str(task_id))
        heartbeat_task = asyncio.create_task(self._beat(job_id, token))
        try:
            task, steps = await self._load(task_id, user_id)
            await execute_task_durable(
                task,
                steps,
                persist=lambda current, target, actor, reason: self._persist(
                    job_id, token, user_id, current, target, actor, reason
                ),
                still_owner=lambda: self._owns(job_id, token),
                mark_step=lambda step, status: self._mark(job_id, token, user_id, step, status),
                runner=lambda step: run_step(user_id, step),
            )
            async with worker_session_scope() as session:
                await complete_job(session, job_id, token)
            _log("job_completed", worker_id=self.worker, job_id=str(job_id), task_id=str(task_id))
        except LeaseLost:
            _log("lease_lost", worker_id=self.worker, job_id=str(job_id), task_id=str(task_id))
        except IntegrationStepError as exc:
            await self._fail(job, str(exc) or "integration_read_failed")
        except RuntimeError as exc:
            code = str(exc) if str(exc) in {
                "approval_required",
                "unknown_agent",
                "empty_plan",
                "illegal_transition",
                "validation_error",
                "not_runnable",
                "step_failed",
            } else "validation_error"
            if code == "step_failed":
                code = "validation_error"
            await self._fail(job, code)
        except Exception:  # noqa: BLE001 - persist unexpected task/provider errors as job failures.
            await self._fail(job, "provider_unavailable")
        finally:
            heartbeat_task.cancel()

    async def _beat(self, job_id, token) -> None:
        while not self.shutdown.is_set():
            await asyncio.sleep(settings.worker_heartbeat_seconds)
            if self.shutdown.is_set():
                return
            async with worker_session_scope() as session:
                alive = await heartbeat(session, job_id, token)
            if not alive:
                await self._cancel_if_requested(job_id, token)
                return

    async def _cancel_if_requested(self, job_id, token) -> None:
        """Fence-cancel after heartbeat returns 0. A lost lease writes nothing."""
        if self.shutdown.is_set():
            return
        async with worker_session_scope() as session:
            await set_worker_guc(session)
            task_id = (
                await session.execute(
                    text("SELECT task_id FROM jobs WHERE id = :id"),
                    {"id": job_id},
                )
            ).scalar_one_or_none()
            if task_id is None:
                return
            cancelled = await cancel_if_requested(session, job_id, token, task_id)
        if cancelled:
            _log("job_cancelled", worker_id=self.worker, job_id=str(job_id), task_id=str(task_id))

    async def _owns(self, job_id, token) -> bool:
        async with worker_session_scope() as session:
            await set_worker_guc(session)
            row = (
                await session.execute(
                    text(
                        """
                        SELECT 1 FROM jobs
                        WHERE id = :id AND lease_token = :token
                          AND status = 'running' AND lease_expires_at > now()
                        """
                    ),
                    {"id": job_id, "token": token},
                )
            ).one_or_none()
            return row is not None

    async def _load(self, task_id, user_id) -> tuple[Task, list[TaskStep]]:
        async with worker_session_scope() as session:
            await set_worker_guc(session)
            await set_tenant_guc(session, user_id)
            task = await session.get(Task, task_id)
            steps = (
                await session.execute(
                    text("SELECT id FROM task_steps WHERE task_id = :id ORDER BY position"),
                    {"id": task_id},
                )
            ).scalars().all()
            loaded = []
            for step_id in steps:
                loaded.append(await session.get(TaskStep, step_id))
            if task is None:
                raise RuntimeError("not_runnable")
            session.expunge(task)
            for step in loaded:
                session.expunge(step)
            return task, loaded

    async def _persist(self, job_id, token, user_id, task, target, actor, reason) -> None:
        async with worker_session_scope() as session:
            await set_worker_guc(session)
            await set_tenant_guc(session, user_id)
            updated = await session.execute(
                text(
                    """
                    UPDATE tasks AS t
                    SET status = :status, updated_at = now(),
                        result = CAST(:result AS jsonb),
                        error = :error
                    FROM jobs j
                    WHERE t.id = :task_id
                      AND j.id = :job_id
                      AND j.task_id = t.id
                      AND j.user_id = :user_id
                      AND j.lease_token = :token
                      AND j.status = 'running'
                      AND j.lease_expires_at > now()
                    """
                ),
                {
                    "status": target.value,
                    "result": json.dumps(task.result) if task.result else None,
                    "error": reason if target.value == "failed" else None,
                    "task_id": task.id,
                    "job_id": job_id,
                    "user_id": user_id,
                    "token": token,
                },
            )
            if updated.rowcount != 1:
                await session.rollback()
                raise LeaseLost
            await session.execute(
                text(
                    """
                    INSERT INTO task_events (
                        id, task_id, from_status, to_status, reason, actor, created_at
                    )
                    SELECT :id, :task_id, :from_status, :to_status, :reason, :actor, now()
                    FROM jobs j
                    WHERE j.id = :job_id AND j.lease_token = :token AND j.status = 'running'
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "task_id": task.id,
                    "from_status": task.status,
                    "to_status": target.value,
                    "reason": reason[:200],
                    "actor": actor,
                    "job_id": job_id,
                    "token": token,
                },
            )
            await session.commit()

    async def _mark(self, job_id, token, user_id, step, status: str) -> None:
        async with worker_session_scope() as session:
            await set_worker_guc(session)
            await set_tenant_guc(session, user_id)
            updated = await session.execute(
                text(
                    """
                    UPDATE task_steps AS s
                    SET status = :status,
                        attempt = CASE WHEN :status = 'running'
                            THEN s.attempt + 1 ELSE s.attempt END,
                        updated_at = now()
                    FROM jobs j
                    WHERE s.id = :step_id
                      AND j.id = :job_id
                      AND j.task_id = s.task_id
                      AND j.user_id = :user_id
                      AND j.lease_token = :token
                      AND j.status = 'running'
                      AND j.lease_expires_at > now()
                    """
                ),
                {
                    "status": status,
                    "step_id": step.id,
                    "job_id": job_id,
                    "user_id": user_id,
                    "token": token,
                },
            )
            if updated.rowcount != 1:
                await session.rollback()
                raise LeaseLost
            await session.commit()

    async def _fail(self, job, code: str) -> None:
        try:
            async with worker_session_scope() as session:
                status = await fail_job(
                    session,
                    job_id=job["id"],
                    token=job["lease_token"],
                    task_id=job["task_id"],
                    error_code=code,
                    attempt_count=job["attempt_count"],
                    max_attempts=job["max_attempts"],
                )
            _log(
                "job_failed",
                worker_id=self.worker,
                job_id=str(job["id"]),
                error_code=code,
                status=status,
            )
        except LeaseLost:
            _log("lease_lost", worker_id=self.worker, job_id=str(job["id"]))


def install_signals(runner: Runner) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, runner.shutdown.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: runner.shutdown.set())


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    runner = Runner(worker_id())
    install_signals(runner)
    await runner.run()
