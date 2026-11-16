"""Devices and durable sync events.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-15

A device is a registered client, never a credential. sync_events is the
append-only record every client replays from, so cross-device state does not
depend on an in-memory stream.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PLATFORMS = ("ios", "android", "web", "desktop", "api")


def upgrade() -> None:
    platform_check = "platform in (" + ",".join(f"'{p}'" for p in PLATFORMS) + ")"
    op.create_table(
        "devices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("platform", sa.Text, nullable=False),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(platform_check, name="devices_platform_valid"),
    )
    op.create_index("devices_user_idx", "devices", ["user_id"])
    op.execute("ALTER TABLE devices ENABLE ROW LEVEL SECURITY")

    op.create_table(
        "sync_events",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("devices.id")),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("sync_events_user_id_idx", "sync_events", ["user_id", "id"])
    op.execute("ALTER TABLE sync_events ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index("sync_events_user_id_idx", table_name="sync_events")
    op.drop_table("sync_events")
    op.drop_index("devices_user_idx", table_name="devices")
    op.drop_table("devices")
