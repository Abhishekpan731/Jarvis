"""Durable jobs and idempotency keys.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_STATUSES = ("queued", "running", "retry_wait", "completed", "failed", "cancelled")


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "tasks_conversation_id_fkey",
        "tasks",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("tasks_conversation_id_idx", "tasks", ["conversation_id"])

    status_check = "status in (" + ",".join(f"'{s}'" for s in JOB_STATUSES) + ")"
    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False
        ),
        sa.Column("job_type", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("worker_id", sa.Text),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(status_check, name="jobs_status_valid"),
        sa.CheckConstraint("job_type = 'task_execution'", name="jobs_type_valid"),
        sa.CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts", name="jobs_attempts_valid"
        ),
        sa.CheckConstraint("max_attempts BETWEEN 1 AND 10", name="jobs_max_attempts_valid"),
        sa.CheckConstraint("priority BETWEEN -10 AND 10", name="jobs_priority_valid"),
        sa.CheckConstraint(
            "(status IN ('completed','failed','cancelled') AND completed_at IS NOT NULL) "
            "OR (status NOT IN ('completed','failed','cancelled'))",
            name="jobs_terminal_completed_at",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND worker_id IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL AND locked_at IS NOT NULL) "
            "OR (status <> 'running')",
            name="jobs_running_lease",
        ),
    )
    op.create_index(
        "jobs_claim_idx",
        "jobs",
        ["priority", "available_at", "created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "jobs_lease_idx",
        "jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index("jobs_user_created_idx", "jobs", ["user_id", sa.text("created_at DESC")])
    op.create_index("jobs_task_created_idx", "jobs", ["task_id", sa.text("created_at DESC")])
    op.create_index(
        "jobs_one_active_per_task",
        "jobs",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running','retry_wait')"),
    )
    op.execute("ALTER TABLE jobs ENABLE ROW LEVEL SECURITY")

    op.create_table(
        "idempotency_keys",
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("request_hash", sa.Text, nullable=False),
        sa.Column(
            "task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("user_id", "key", name="idempotency_keys_pkey"),
    )
    op.execute("ALTER TABLE idempotency_keys ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.execute("DROP INDEX IF EXISTS jobs_one_active_per_task")
    op.execute("DROP INDEX IF EXISTS jobs_task_created_idx")
    op.execute("DROP INDEX IF EXISTS jobs_user_created_idx")
    op.execute("DROP INDEX IF EXISTS jobs_lease_idx")
    op.execute("DROP INDEX IF EXISTS jobs_claim_idx")
    op.drop_table("jobs")
    op.drop_index("tasks_conversation_id_idx", table_name="tasks")
    op.drop_constraint("tasks_conversation_id_fkey", "tasks", type_="foreignkey")
    op.drop_column("tasks", "conversation_id")
