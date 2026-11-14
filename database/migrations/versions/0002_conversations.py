"""Conversations, messages, and memories.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("summary", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status in ('active', 'archived')", name="conversations_status_valid"),
    )
    op.create_index("conversations_user_updated_idx", "conversations", ["user_id", "updated_at"])

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_type", sa.Text, nullable=False, server_default="text"),
        sa.Column("token_usage", postgresql.JSONB),
        sa.Column("provider", sa.Text),
        sa.Column("model", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "role in ('user', 'assistant', 'system', 'tool', 'error')",
            name="messages_role_valid",
        ),
    )
    op.create_index("messages_conversation_created_idx", "messages", ["conversation_id", "created_at"])

    op.create_table(
        "memories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("privacy_class", sa.Text, nullable=False, server_default="normal"),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("source_conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id")),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("messages.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "category in ('profile', 'preferences', 'work', 'projects', 'goals',"
            "'knowledge', 'decisions', 'routines', 'summaries', 'instructions')",
            name="memories_category_valid",
        ),
        sa.CheckConstraint("privacy_class in ('normal', 'sensitive')", name="memories_privacy_valid"),
    )
    op.create_index("memories_user_category_idx", "memories", ["user_id", "category"])

    # The column exists so a later phase can store embeddings. Nothing writes it yet.
    op.execute("alter table memories add column embedding vector(1536)")

    for table in ("conversations", "messages", "memories"):
        op.execute(f"alter table {table} enable row level security")
        op.execute(
            f"create policy {table}_owner on {table} "
            "using (user_id = current_setting('app.user_id', true)::uuid)"
        )


def downgrade() -> None:
    op.drop_table("memories")
    op.drop_table("messages")
    op.drop_table("conversations")
