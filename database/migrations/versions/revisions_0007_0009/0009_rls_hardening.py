"""RLS hardening: non-owner runtime role, FORCE RLS, WITH CHECK, worker escape hatch.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-15

Owner-bypass meant RLS was never evaluated: the app connected as `jarvis`,
the same role that owns every table. This migration creates a non-owner
runtime role (`jarvis_app`) that the application connects as from now on,
forces RLS on every tenant-owned table (owners still bypass RLS unless it
is FORCED), and adds WITH CHECK clauses so a row cannot be inserted or
updated into a tenant it does not belong to. `task_steps`, `task_events`,
`jobs`, `idempotency_keys`, `devices`, and `sync_events` had RLS enabled
with zero policies; they get policies here. The worker claims jobs across
tenants by design, so policies also accept `current_setting('app.worker',
true) = 'true'`, a GUC the worker sets with `SET LOCAL` before cross-tenant
queue operations.

Non-tenant tables (`users`, `sessions`, `user_preferences`, `agents`,
`agent_capabilities`, `audit_logs`, `model_usage`) are left alone.
"""

import os
from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "jarvis_app"
OWNER_ROLE = "jarvis"

# Tables with a direct `user_id` column, USING/WITH CHECK via that column.
DIRECT_TENANT_TABLES = (
    "tasks",
    "conversations",
    "messages",
    "memories",
    "jobs",
    "idempotency_keys",
    "devices",
    "sync_events",
    "integrations",
    "oauth_states",
    "notifications",
    "automations",
)

# Tables with no user_id column; tenancy is derived via tasks.id = task_id.
JOINED_TENANT_TABLES = ("task_steps", "task_events")

# Existing (inert) policies that get dropped and recreated with WITH CHECK.
EXISTING_OWNER_POLICIES = {
    "tasks": "tasks_owner",
    "conversations": "conversations_owner",
    "messages": "messages_owner",
    "memories": "memories_owner",
    "integrations": "integrations_owner",
    "oauth_states": "oauth_states_owner",
    "notifications": "notifications_owner",
    "automations": "automations_owner",
}

# jobs, idempotency_keys, task_steps, task_events also honor the worker GUC,
# per the cross-tenant job queue. automations/notifications are added for the
# same reason: the worker's daily-briefing sweep (app/briefing.py run_due)
# scans every user's due schedule and writes a notification for each one in
# a single transaction, which is the same cross-tenant shape as job claiming.
WORKER_ESCAPE_TABLES = (
    "jobs",
    "idempotency_keys",
    "task_steps",
    "task_events",
    "automations",
    "notifications",
)

FORCE_RLS_TABLES = DIRECT_TENANT_TABLES + JOINED_TENANT_TABLES


def _app_password() -> str:
    """Dev-only fallback password. Set JARVIS_APP_DB_PASSWORD (or the Alembic
    `app_db_password` ini option) in every real environment; this fallback is
    never safe outside a local dev database."""
    configured = op.get_context().config.get_main_option("app_db_password")
    if configured:
        return configured
    return os.environ.get("JARVIS_APP_DB_PASSWORD", "change-me-app")


def _sql_literal(value: str) -> str:
    """Quote a value for a SQL string literal. Rejects NUL, which cannot appear."""
    if "\x00" in value:
        raise RuntimeError("database password contains a NUL byte")
    return "'" + value.replace("'", "''") + "'"


def _tenant_predicate(table: str) -> str:
    # NULLIF guard: once a custom GUC has been SET (even SET LOCAL) on a
    # physical connection, Postgres resets it to '' (not NULL) when the
    # transaction ends, since the connection's placeholder for it now has a
    # boot value of ''. A pooled connection that previously scoped a tenant
    # will therefore return '' here on any later transaction that forgets
    # to set app.user_id, and ''::uuid raises instead of failing closed.
    # NULLIF(..., '') turns that '' back into NULL so the cast yields NULL
    # (no match) instead of erroring.
    if table in JOINED_TENANT_TABLES:
        return (
            f"EXISTS (SELECT 1 FROM tasks t WHERE t.id = {table}.task_id "
            "AND t.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)"
        )
    return "user_id = NULLIF(current_setting('app.user_id', true), '')::uuid"

def _predicate(table: str) -> str:
    tenant = _tenant_predicate(table)
    if table in WORKER_ESCAPE_TABLES:
        return f"(current_setting('app.worker', true) = 'true' OR {tenant})"
    return tenant


def upgrade() -> None:
    password = _sql_literal(_app_password())
    # CREATE ROLE cannot take a bound parameter. The password is a quoted
    # literal, and ALTER ROLE always runs so a changed password is applied
    # on a database where the role already exists.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN PASSWORD {password} NOINHERIT;
            END IF;
        END
        $$;
        ALTER ROLE {APP_ROLE} WITH LOGIN PASSWORD {password};
        """
    )

    bind = op.get_bind()
    dbname = bind.engine.url.database or OWNER_ROLE
    op.execute(f'GRANT CONNECT ON DATABASE "{dbname}" TO {APP_ROLE}')
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )

    for table in FORCE_RLS_TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    for table, policy in EXISTING_OWNER_POLICIES.items():
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        predicate = _predicate(table)
        op.execute(
            f"CREATE POLICY {policy} ON {table} USING ({predicate}) WITH CHECK ({predicate})"
        )

    for table in ("jobs", "idempotency_keys", "devices", "sync_events", *JOINED_TENANT_TABLES):
        predicate = _predicate(table)
        op.execute(f"DROP POLICY IF EXISTS {table}_owner ON {table}")
        op.execute(
            f"CREATE POLICY {table}_owner ON {table} USING ({predicate}) WITH CHECK ({predicate})"
        )


def downgrade() -> None:
    for table in ("jobs", "idempotency_keys", "devices", "sync_events", *JOINED_TENANT_TABLES):
        op.execute(f"DROP POLICY {table}_owner ON {table}")

    for table, policy in EXISTING_OWNER_POLICIES.items():
        op.execute(f"DROP POLICY {policy} ON {table}")
        op.execute(
            f"CREATE POLICY {policy} ON {table} "
            "USING (user_id = current_setting('app.user_id', true)::uuid)"
        )

    for table in FORCE_RLS_TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")

    bind = op.get_bind()
    dbname = bind.engine.url.database or OWNER_ROLE
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}"
    )
    op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
    op.execute(f'REVOKE CONNECT ON DATABASE "{dbname}" FROM {APP_ROLE}')
    op.execute(f"DROP ROLE IF EXISTS {APP_ROLE}")
