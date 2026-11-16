"""Reminder text on a user-defined schedule.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-15

The daily briefing has a fixed body. A reminder the user wrote does not, so
the text lives on the schedule and is copied into the notification when due.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "automations",
        sa.Column("body", sa.Text, nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("automations", "body")
