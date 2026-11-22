"""Move cross-tenant worker access from a caller-settable GUC to DB identity.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-22
"""

import os
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "jarvis_app"
OWNER_ROLE = "jarvis"
WORKER_ROLE = "jarvis_worker"
POLICY_TABLES = (
    "jobs",
    "idempotency_keys",
    "task_steps",
    "task_events",
    "automations",
    "notifications",
)
WORKER_BYPASS_TABLES = frozenset(
    {"jobs", "task_steps", "task_events", "automations", "notifications"}
)
JOINED_TENANT_TABLES = frozenset({"task_steps", "task_events"})



def _password(option_name: str, env_name: str) -> str:
    """Require a configured DB password; never create a known worker secret."""
    value = op.get_context().config.get_main_option(option_name) or os.environ.get(env_name)
    if not value:
        raise RuntimeError(f"{env_name} is required to provision the worker DB role")
    if "\x00" in value:
        raise RuntimeError(f"{env_name} contains an invalid NUL byte")
    return value


def _sql_literal(value: str) -> str:
    """Quote a value for a SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def _tenant_predicate(table: str) -> str:
    """Build the existing per-tenant predicate for an RLS table."""
    if table in JOINED_TENANT_TABLES:
        return (
            f"EXISTS (SELECT 1 FROM tasks t WHERE t.id = {table}.task_id "
            "AND t.user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)"
        )
    return "user_id = NULLIF(current_setting('app.user_id', true), '')::uuid"


def _predicate(table: str, allow_worker: bool) -> str:
    """Use authenticated session identity only for the narrow worker tables."""
    tenant = _tenant_predicate(table)
    if allow_worker:
        return f"(session_user = '{WORKER_ROLE}' OR {tenant})"
    return tenant


def _set_policy(table: str, allow_worker: bool) -> None:
    """Replace a table's RLS policy without changing its tenant condition."""
    _replace_policy(table, _predicate(table, allow_worker))



def _replace_policy(table: str, predicate: str) -> None:
    """Drop and recreate the existing policy name with the supplied predicate."""
    policy = f"{table}_owner"
    op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    op.execute(
        f"CREATE POLICY {policy} ON {table} USING ({predicate}) WITH CHECK ({predicate})"
    )


def _assert_role_separation() -> None:
    """Reject memberships that would let either service assume the other's role."""
    result = op.get_bind().execute(
        text(
            "WITH RECURSIVE api_roles(role_oid) AS ("
            "SELECT oid FROM pg_roles WHERE rolname = :app UNION "
            "SELECT m.roleid FROM pg_auth_members m JOIN api_roles r ON m.member = r.role_oid), "
            "worker_roles(role_oid) AS ("
            "SELECT oid FROM pg_roles WHERE rolname = :worker UNION "
            "SELECT m.roleid FROM pg_auth_members m JOIN worker_roles r ON m.member = r.role_oid) "
            "SELECT EXISTS (SELECT 1 FROM api_roles WHERE role_oid = "
            "(SELECT oid FROM pg_roles WHERE rolname = :worker)) AS app_can_assume_worker, "
            "EXISTS (SELECT 1 FROM api_roles WHERE role_oid = "
            "(SELECT oid FROM pg_roles WHERE rolname = :owner)) AS app_can_assume_owner, "
            "EXISTS (SELECT 1 FROM api_roles r JOIN pg_roles p ON p.oid = r.role_oid "
            "WHERE p.rolsuper OR p.rolcreaterole OR p.rolcreatedb OR p.rolbypassrls) "
            "AS app_has_elevated_membership, "
            "EXISTS (SELECT 1 FROM worker_roles WHERE role_oid <> "
            "(SELECT oid FROM pg_roles WHERE rolname = :worker)) AS worker_has_memberships, "
            "EXISTS (SELECT 1 FROM worker_roles r JOIN pg_roles p ON p.oid = r.role_oid "
            "WHERE p.rolsuper OR p.rolcreaterole OR p.rolcreatedb OR p.rolbypassrls) "
            "AS worker_has_elevated_membership"
        ),
        {"app": APP_ROLE, "owner": OWNER_ROLE, "worker": WORKER_ROLE},
    ).mappings().one()
    if any(result.values()):
        raise RuntimeError(
            f"DB role memberships violate API/worker isolation; remove {APP_ROLE} memberships "
            f"in {OWNER_ROLE}/{WORKER_ROLE} and worker memberships or elevated inherited roles"
        )


def _assert_worker_role_absent() -> None:
    """Fail before mutation when the worker role already exists."""
    role_exists = (
        op.get_bind()
        .execute(
            text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
            {"role": WORKER_ROLE},
        )
        .scalar_one()
    )
    if role_exists:
        raise RuntimeError(
            f"{WORKER_ROLE} already exists; rename or remove it before applying this migration. "
            "The migration will not alter or later drop a pre-existing worker role."
        )


def _assert_no_public_worker_privilege_paths() -> None:
    """Reject PUBLIC schema/sequence grants instead of changing other roles' access."""
    result = (
        op.get_bind()
        .execute(
            text(
                "SELECT "
                "EXISTS (SELECT 1 FROM pg_namespace n "
                "CROSS JOIN LATERAL aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) a "
                "WHERE n.nspname = 'public' AND a.grantee = 0 "
                "AND a.privilege_type = 'CREATE') AS public_schema_create, "
                "EXISTS (SELECT 1 FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl, acldefault('S', c.relowner))) a "
                "WHERE n.nspname = 'public' AND c.relkind = 'S' AND a.grantee = 0) "
                "AS public_sequence_privilege, "
                "EXISTS (SELECT 1 FROM pg_default_acl d "
                "CROSS JOIN LATERAL aclexplode(d.defaclacl) a "
                "WHERE d.defaclobjtype = 'S' AND a.grantee = 0 "
                "AND (d.defaclnamespace = 0 OR d.defaclnamespace = "
                "(SELECT oid FROM pg_namespace WHERE nspname = 'public'))) "
                "AS public_default_sequence_privilege"
            )
        )
        .mappings()
        .one()
    )
    if any(result.values()):
        raise RuntimeError(
            "PUBLIC/default privileges expose CREATE on schema public or sequence privileges; "
            "remove the PUBLIC grant before provisioning jarvis_worker. The migration will not "
            "revoke access from unrelated roles."
        )


def _grant_worker_access() -> None:
    """Grant only the operations used by worker execution and briefing code."""
    bind = op.get_bind()
    database = bind.engine.url.database or OWNER_ROLE
    quoted_database = bind.dialect.identifier_preparer.quote(database)
    op.execute(f"GRANT CONNECT ON DATABASE {quoted_database} TO {WORKER_ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {WORKER_ROLE}")
    op.execute(
        f"GRANT SELECT, UPDATE ON TABLE jobs, tasks, task_steps, automations, integrations "
        f"TO {WORKER_ROLE}"
    )
    op.execute(f"GRANT INSERT ON TABLE task_events TO {WORKER_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON TABLE notifications TO {WORKER_ROLE}")


def upgrade() -> None:
    """Provision an isolated worker login and replace GUC-based RLS policies."""
    _assert_worker_role_absent()
    _assert_no_public_worker_privilege_paths()
    app_password = _password("app_db_password", "JARVIS_APP_DB_PASSWORD")
    worker_password = _password("worker_db_password", "JARVIS_WORKER_DB_PASSWORD")
    if app_password == worker_password:
        raise RuntimeError("API and worker database passwords must be different")
    password_literal = _sql_literal(worker_password)
    op.execute(
        f"CREATE ROLE {WORKER_ROLE} LOGIN PASSWORD {password_literal} "
        "NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
    )
    op.execute(
        f"ALTER ROLE {APP_ROLE} WITH LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
    )
    _assert_role_separation()
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {WORKER_ROLE}")
    op.execute(f"REVOKE ALL PRIVILEGES ON SCHEMA public FROM {WORKER_ROLE}")
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {WORKER_ROLE}")
    _grant_worker_access()
    for table in POLICY_TABLES:
        _set_policy(table, table in WORKER_BYPASS_TABLES)


def downgrade() -> None:
    """Restore tenant-only policies and remove the migration-created worker role."""
    for table in POLICY_TABLES:
        _set_policy(table, False)
    op.execute(
        f"REVOKE ALL PRIVILEGES ON TABLE {', '.join(POLICY_TABLES)}, tasks, integrations "
        f"FROM {WORKER_ROLE}"
    )
    op.execute(f"REVOKE ALL PRIVILEGES ON SCHEMA public FROM {WORKER_ROLE}")
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {WORKER_ROLE}")
    bind = op.get_bind()
    database = bind.engine.url.database or OWNER_ROLE
    quoted_database = bind.dialect.identifier_preparer.quote(database)
    op.execute(f"REVOKE CONNECT ON DATABASE {quoted_database} FROM {WORKER_ROLE}")
    op.execute(f"DROP ROLE {WORKER_ROLE}")