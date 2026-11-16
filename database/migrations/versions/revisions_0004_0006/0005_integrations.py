"""OAuth integration connections.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-15

A connection stores an encrypted token blob, never a plaintext access or refresh
token. The state row binds an authorize redirect to the user who started it, so
a callback cannot be attached to a different account.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROVIDERS = ("gmail", "google_calendar")


def upgrade() -> None:
    provider_check = "provider in (" + ",".join(f"'{name}'" for name in PROVIDERS) + ")"
    op.create_table(
        "integrations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("scopes", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("account_label", sa.Text),
        sa.Column("token_ciphertext", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("connected_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(provider_check, name="integrations_provider_valid"),
        sa.CheckConstraint("status in ('pending', 'connected', 'revoked')", name="integrations_status_valid"),
        sa.UniqueConstraint("user_id", "provider", name="integrations_user_provider_unique"),
    )
    op.create_index("integrations_user_idx", "integrations", ["user_id"])
    op.execute("alter table integrations enable row level security")
    op.execute(
        "create policy integrations_owner on integrations "
        "using (user_id = current_setting('app.user_id', true)::uuid)"
    )

    op.create_table(
        "oauth_states",
        sa.Column("state", sa.Text, primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("code_verifier", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(provider_check, name="oauth_states_provider_valid"),
    )
    op.create_index("oauth_states_user_idx", "oauth_states", ["user_id"])
    op.execute("alter table oauth_states enable row level security")
    op.execute(
        "create policy oauth_states_owner on oauth_states "
        "using (user_id = current_setting('app.user_id', true)::uuid)"
    )


def downgrade() -> None:
    op.drop_table("oauth_states")
    op.drop_table("integrations")
