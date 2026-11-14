"""ORM models for the Phase 1 schema.

These mirror the Alembic migration. When they disagree, the migration wins —
it is what the database actually does.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

_TZ = DateTime(timezone=True)

from app.db import Base


def _uuid() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid()
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        _TZ, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(_TZ)


class UserPreferences(Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="UTC")
    briefing_language: Mapped[str] = mapped_column(Text, nullable=False, server_default="en")
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=func.now())


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _uuid()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    refresh_token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_agent: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(_TZ, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(_TZ)
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=func.now())


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(nullable=False)
    max_retries: Mapped[int] = mapped_column(nullable=False)
    risk_ceiling: Mapped[str] = mapped_column(Text, nullable=False)


class AgentCapability(Base):
    __tablename__ = "agent_capabilities"

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), primary_key=True)
    capability: Mapped[str] = mapped_column(Text, primary_key=True)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status in ('created','understanding','planning','waiting_for_approval',"
            "'executing','verifying','completed','failed','cancelled','paused',"
            "'waiting_for_user','requires_retry')",
            name="tasks_status_valid",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="created")
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    heartbeat_at: Mapped[datetime | None] = mapped_column(_TZ)
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        _TZ, server_default=func.now(), onupdate=func.now()
    )


class TaskStep(Base):
    __tablename__ = "task_steps"
    __table_args__ = (UniqueConstraint("task_id", "position"),)

    id: Mapped[uuid.UUID] = _uuid()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(nullable=False)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    risk: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attempt: Mapped[int] = mapped_column(nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        _TZ, server_default=func.now(), onupdate=func.now()
    )


class TaskEvent(Base):
    """Append-only record of every state transition."""

    __tablename__ = "task_events"

    id: Mapped[uuid.UUID] = _uuid()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=func.now())


class Job(Base):
    """Durable queue row. Isolation is user_id in API queries, not RLS."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status in ('queued','running','retry_wait','completed','failed','cancelled')",
            name="jobs_status_valid",
        ),
        CheckConstraint("job_type = 'task_execution'", name="jobs_type_valid"),
        CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts", name="jobs_attempts_valid"
        ),
        CheckConstraint("max_attempts BETWEEN 1 AND 10", name="jobs_max_attempts_valid"),
        CheckConstraint("priority BETWEEN -10 AND 10", name="jobs_priority_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    job_type: Mapped[str] = mapped_column(Text, nullable=False, default="task_execution")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    available_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(_TZ)
    lease_expires_at: Mapped[datetime | None] = mapped_column(_TZ)
    worker_id: Mapped[str | None] = mapped_column(Text)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(_TZ)
    last_error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(_TZ)
    completed_at: Mapped[datetime | None] = mapped_column(_TZ)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=func.now())


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = _uuid()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        _TZ, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(_TZ)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = _uuid()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="text")
    token_usage: Mapped[dict | None] = mapped_column(JSONB)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=func.now())


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = _uuid()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    privacy_class: Mapped[str] = mapped_column(Text, nullable=False, server_default="normal")
    active: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        _TZ, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(_TZ)


class AuditLog(Base):
    """Append-only. The application role has no update or delete grant."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = _uuid()
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(_TZ, server_default=func.now())


class Device(Base):
    """A registered client. A device id is not a credential."""

    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(_TZ)
    revoked_at: Mapped[datetime | None] = mapped_column(_TZ)


class SyncEvent(Base):
    """Append-only per-user event log. Clients replay from the last id seen."""

    __tablename__ = "sync_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(_TZ, nullable=False, server_default=func.now())
