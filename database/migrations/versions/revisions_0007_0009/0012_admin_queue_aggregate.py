"""Expose only aggregate queue counts to authenticated admin code.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-22

Upgrade requires CREATEROLE, CREATE on schema public, and ownership of public.jobs (or
superuser privileges). Downgrade requires a superuser because PostgreSQL 16 requires ADMIN
OPTION to grant membership, and the migration intentionally leaves no memberships behind.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "jarvis_app"
WORKER_ROLE = "jarvis_worker"
AGGREGATE_ROLE = "jarvis_queue_aggregator"


def _assert_aggregate_role_absent() -> None:
    """Reject a pre-existing role so downgrade cannot remove unrelated state."""
    role_exists = op.get_bind().execute(
        text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
        {"role": AGGREGATE_ROLE},
    ).scalar_one()
    if role_exists:
        raise RuntimeError(
            f"{AGGREGATE_ROLE} already exists; rename or remove it before applying this migration. "
            "The migration will not reuse or later drop a pre-existing role."
        )


def _require_upgrade_privileges() -> str:
    """Require only the rights needed to create the role and secure jobs."""
    result = op.get_bind().execute(
        text(
            "SELECT r.rolname AS executor, r.rolsuper AS is_superuser, "
            "r.rolcreaterole AS can_create_roles, "
            "has_schema_privilege(r.rolname, 'public', 'CREATE') AS can_create_in_public, "
            "EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = 'jobs' "
            "AND c.relowner = r.oid) AS owns_jobs "
            "FROM pg_roles r WHERE r.rolname = current_user"
        )
    ).mappings().one()
    if not result["is_superuser"] and not all(
        result[key] for key in ("can_create_roles", "can_create_in_public", "owns_jobs")
    ):
        raise RuntimeError(
            "queue aggregate migration upgrade requires CREATEROLE, CREATE on schema public, "
            "and ownership of public.jobs; alternatively run it as a PostgreSQL superuser."
        )
    return result["executor"]



def _require_downgrade_superuser() -> str:
    """Require superuser to create a removable membership during downgrade."""
    result = op.get_bind().execute(
        text(
            "SELECT current_user AS executor, r.rolsuper AS is_superuser "
            "FROM pg_roles r WHERE r.rolname = current_user"
        )
    ).mappings().one()
    if not result["is_superuser"]:
        raise RuntimeError(
            "queue aggregate migration downgrade requires a PostgreSQL superuser: the role "
            "has no memberships by design, and PostgreSQL 16 requires ADMIN OPTION to grant "
            "the temporary SET ROLE membership; CREATEROLE alone is insufficient."
        )
    return result["executor"]


def _assert_role_has_no_memberships() -> None:
    """Ensure nobody can assume or inherit the non-login aggregate role."""
    has_memberships = op.get_bind().execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_auth_members "
            "WHERE roleid = (SELECT oid FROM pg_roles WHERE rolname = :role) "
            "OR member = (SELECT oid FROM pg_roles WHERE rolname = :role))"
        ),
        {"role": AGGREGATE_ROLE},
    ).scalar_one()
    if has_memberships:
        raise RuntimeError("queue aggregate role must have no role memberships")


def _quoted_executor(role_name: str) -> str:
    """Quote the connected migration executor as a SQL identifier."""
    return op.get_bind().dialect.identifier_preparer.quote(role_name)


def _grant_temporary_membership(executor: str) -> None:
    """Allow SET ROLE only, without inheriting aggregate privileges."""
    op.execute(
        f"GRANT {AGGREGATE_ROLE} TO {_quoted_executor(executor)} WITH INHERIT FALSE, SET TRUE"
    )


def _revoke_temporary_membership(executor: str) -> None:
    """Remove the migration executor's temporary aggregate-role membership."""
    op.execute(f"REVOKE {AGGREGATE_ROLE} FROM {_quoted_executor(executor)}")


def _assert_temporary_membership(executor: str, admin_option: bool) -> None:
    """Verify the executor is the sole temporary member with the expected grant options."""
    valid_membership = op.get_bind().execute(
        text(
            "SELECT count(*) = 1 AND bool_or("
            "m.member = (SELECT oid FROM pg_roles WHERE rolname = :executor) "
            "AND m.admin_option = :admin_option AND m.set_option "
            "AND NOT m.inherit_option) "
            "FROM pg_auth_members m "
            "WHERE m.roleid = (SELECT oid FROM pg_roles WHERE rolname = :role) "
            "OR m.member = (SELECT oid FROM pg_roles WHERE rolname = :role)"
        ),
        {"executor": executor, "role": AGGREGATE_ROLE, "admin_option": admin_option},
    ).scalar_one()
    if not valid_membership:
        raise RuntimeError(
            "queue aggregate role must have only the migration executor as a temporary "
            "non-inheriting SET member with expected ADMIN OPTION"
        )



def _provision_aggregate_role(executor: str) -> None:
    """Create a non-login aggregate role with temporary executor membership."""
    op.execute(
        f"CREATE ROLE {AGGREGATE_ROLE} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
        f"NOCREATEROLE NOREPLICATION NOBYPASSRLS ADMIN {_quoted_executor(executor)}"
    )
    op.execute(
        f"GRANT {AGGREGATE_ROLE} TO {_quoted_executor(executor)} "
        "WITH INHERIT FALSE, SET TRUE, ADMIN TRUE"
    )
    _assert_temporary_membership(executor, admin_option=True)
    op.execute(f"GRANT USAGE ON SCHEMA public TO {AGGREGATE_ROLE}")



def _create_aggregate_function(executor: str) -> None:
    """Restrict function execution before transferring ownership."""
    op.execute(
        """
        CREATE FUNCTION public.admin_queue_depth()
        RETURNS TABLE (queued bigint, running bigint, retry_wait bigint, failed bigint)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $queue$
            SELECT count(*) FILTER (WHERE status = 'queued') AS queued,
                   count(*) FILTER (WHERE status = 'running') AS running,
                   count(*) FILTER (WHERE status = 'retry_wait') AS retry_wait,
                   count(*) FILTER (WHERE status = 'failed') AS failed
            FROM public.jobs
        $queue$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.admin_queue_depth() FROM PUBLIC")
    op.execute(f"REVOKE ALL ON FUNCTION public.admin_queue_depth() FROM {WORKER_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.admin_queue_depth() TO {APP_ROLE}")
    op.execute(f"GRANT CREATE ON SCHEMA public TO {AGGREGATE_ROLE}")
    op.execute(f"ALTER FUNCTION public.admin_queue_depth() OWNER TO {AGGREGATE_ROLE}")
    _revoke_temporary_membership(executor)
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {AGGREGATE_ROLE}")
    _assert_role_has_no_memberships()



def _grant_aggregate_access() -> None:
    """Grant only queue reads under the aggregate role's RLS policy."""
    op.execute(f"GRANT SELECT ON TABLE public.jobs TO {AGGREGATE_ROLE}")
    op.execute(
        f"CREATE POLICY jobs_queue_aggregate ON public.jobs "
        f"FOR SELECT TO {AGGREGATE_ROLE} USING (true)"
    )

def upgrade() -> None:
    """Grant the API role execution of a queue-count-only definer function."""
    _assert_aggregate_role_absent()
    executor = _require_upgrade_privileges()
    _provision_aggregate_role(executor)
    _create_aggregate_function(executor)
    _grant_aggregate_access()



def downgrade() -> None:
    """Drop the function with a temporary SET ROLE membership and remove its role."""
    executor = _require_downgrade_superuser()
    _assert_role_has_no_memberships()
    op.execute("DROP POLICY jobs_queue_aggregate ON public.jobs")
    op.execute(
        f"REVOKE EXECUTE ON FUNCTION public.admin_queue_depth() FROM {APP_ROLE}, "
        f"{WORKER_ROLE}, PUBLIC"
    )
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE public.jobs FROM {AGGREGATE_ROLE}")
    _grant_temporary_membership(executor)
    _assert_temporary_membership(executor, admin_option=False)
    op.execute(f"SET ROLE {AGGREGATE_ROLE}")
    op.execute("DROP FUNCTION public.admin_queue_depth()")
    op.execute("RESET ROLE")
    _revoke_temporary_membership(executor)
    _assert_role_has_no_memberships()
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {AGGREGATE_ROLE}")
    op.execute(f"DROP ROLE {AGGREGATE_ROLE}")
