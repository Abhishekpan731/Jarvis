"""Token counts. No prompt text and no reply text.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-15

A row records how many tokens one model call used. The admin view reads the
sums. It does not read conversations, memory, or task instructions.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="usage_tokens_valid"),
    )
    op.create_index("model_usage_created", "model_usage", ["created_at"])


def downgrade() -> None:
    op.drop_index("model_usage_created", table_name="model_usage")
    op.drop_table("model_usage")
