"""In-app notifications and user-defined schedules.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-15

Delivery is a row the signed-in user pulls. Nothing here sends mail or talks
to a device. A schedule names when a briefing should be written; the worker
inserts that row. Email delivery stays unconfigured until a mail credential
exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = ("briefing", "task", "automation")
CHANNELS = ("in_app",)


def upgrade() -> None:
    kind_check = "kind in (" + ",".join(f"'{name}'" for name in KINDS) + ")"
    channel_check = "channel in (" + ",".join(f"'{name}'" for name in CHANNELS) + ")"
    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("channel", sa.Text, nullable=False, server_default="in_app"),
        sa.Column("dedupe_key", sa.Text),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(kind_check, name="notifications_kind_valid"),
        sa.CheckConstraint(channel_check, name="notifications_channel_valid"),
        sa.UniqueConstraint("user_id", "dedupe_key", name="notifications_user_dedupe_unique"),
    )
    op.create_index("notifications_user_unread_idx", "notifications", ["user_id", "read_at"])
    op.execute("alter table notifications enable row level security")
    op.execute(
        "create policy notifications_owner on notifications "
        "using (user_id = current_setting('app.user_id', true)::uuid)"
    )

    op.create_table(
        "automations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("hour", sa.Integer, nullable=False),
        sa.Column("minute", sa.Integer, nullable=False, server_default="0"),
        sa.Column("timezone", sa.Text, nullable=False, server_default="UTC"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("last_slot", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("hour between 0 and 23", name="automations_hour_valid"),
        sa.CheckConstraint("minute between 0 and 59", name="automations_minute_valid"),
        sa.UniqueConstraint("user_id", "name", name="automations_user_name_unique"),
    )
    op.create_index("automations_user_idx", "automations", ["user_id"])
    op.execute("alter table automations enable row level security")
    op.execute(
        "create policy automations_owner on automations "
        "using (user_id = current_setting('app.user_id', true)::uuid)"
    )


def downgrade() -> None:
    op.drop_table("automations")
    op.drop_table("notifications")
