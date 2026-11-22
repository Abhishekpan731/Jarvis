"""Close audited approval, memory, rate-limit, and audit-log gaps.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "jarvis_app"


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("approval_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("tasks", sa.Column("approval_action_hash", sa.Text, nullable=True))

    op.add_column(
        "memories",
        sa.Column(
            "status",
            sa.Text,
            nullable=False,
            server_default="proposed",
        ),
    )
    op.execute(
        "ALTER TABLE memories ADD CONSTRAINT memories_status_valid "
        "CHECK (status in ('proposed', 'confirmed')) NOT VALID"
    )
    op.execute("ALTER TABLE memories VALIDATE CONSTRAINT memories_status_valid")
    op.alter_column("memories", "privacy_class", server_default="sensitive")
    op.execute(
        "UPDATE memories SET status = 'proposed', privacy_class = 'sensitive', "
        "embedding = NULL"
    )
    op.create_table(
        "rate_limit_windows",
        sa.Column("bucket_key", sa.Text, nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "request_count",
            sa.Integer,
            nullable=False,
            server_default="1",
        ),
        sa.CheckConstraint(
            "request_count > 0",
            name="rate_limit_windows_count_positive",
        ),
        sa.PrimaryKeyConstraint(
            "bucket_key",
            "window_start",
            name="rate_limit_windows_pkey",
        ),
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE rate_limit_windows TO {APP_ROLE}"
    )
    op.execute(f"REVOKE UPDATE, DELETE ON TABLE audit_logs FROM {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"GRANT UPDATE, DELETE ON TABLE audit_logs TO {APP_ROLE}")
    op.drop_table("rate_limit_windows")
    op.drop_constraint("memories_status_valid", "memories", type_="check")
    op.drop_column("memories", "status")
    op.drop_column("tasks", "approval_action_hash")
    op.drop_column("tasks", "approval_expires_at")
