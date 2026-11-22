"""RLS boundary tests using explicitly isolated API and worker logins."""

import os
import uuid
from pathlib import Path

import pytest
from app.config import settings
from app.db import session_scope, set_tenant_guc, worker_session_scope
from app.jobs import insert_job
from app.models import Task, User
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[2]
    / "database/migrations/versions/revisions_0007_0009"
)


def test_worker_migration_rejects_reused_role_and_public_privilege_paths():
    """Fail closed on inherited PUBLIC ACLs without changing unrelated roles."""
    source = (MIGRATIONS_DIR / "0011_worker_role_rls.py").read_text()
    upgrade = source[source.index("def upgrade()") :]
    assert upgrade.index("_assert_worker_role_absent()") < upgrade.index(
        "_assert_no_public_worker_privilege_paths()"
    ) < upgrade.index("CREATE ROLE {WORKER_ROLE}")
    assert "ALTER ROLE {WORKER_ROLE}" not in source
    assert "DROP ROLE {WORKER_ROLE}" in source
    assert "DROP ROLE IF EXISTS" not in source
    assert "pg_default_acl" in source
    assert "acldefault('n', n.nspowner)" in source
    assert "acldefault('S', c.relowner)" in source
    assert "a.grantee = 0" in source
    assert "remove the PUBLIC grant" in source
    assert "REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC" not in source
    assert "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC" not in source
    assert "REVOKE ALL PRIVILEGES ON SCHEMA public FROM {WORKER_ROLE}" in source
    assert "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {WORKER_ROLE}" in source
    worker_grants = source[source.index("def _grant_worker_access()") : source.index("def upgrade()")]
    assert "SEQUENCE" not in worker_grants
    for membership_check in (
        "app_can_assume_worker",
        "app_can_assume_owner",
        "app_has_elevated_membership",
        "worker_has_memberships",
        "worker_has_elevated_membership",
    ):
        assert membership_check in source



def test_worker_migration_downgrade_does_not_restore_guc_bypass():
    """Check downgrade retains identity-based access rather than trusting a GUC."""
    source = (MIGRATIONS_DIR / "0011_worker_role_rls.py").read_text()
    assert "app.worker" not in source
    assert "_set_legacy_policy" not in source



def test_queue_aggregate_migration_uses_temporary_membership_and_clear_prerequisites():
    """Check ownership transfer membership is temporary and safely bounded."""
    source = (MIGRATIONS_DIR / "0012_admin_queue_aggregate.py").read_text()
    assert "_assert_aggregate_role_absent()" in source
    assert "queue aggregate migration upgrade requires CREATEROLE" in source
    assert "CREATE on schema public" in source
    assert "ownership of public.jobs" in source
    assert "queue aggregate migration downgrade requires a PostgreSQL superuser" in source
    assert "PostgreSQL 16 requires ADMIN OPTION" in source
    assert "CREATEROLE alone is insufficient" in source
    assert "CREATE ROLE {AGGREGATE_ROLE} NOLOGIN" in source
    assert "ADMIN {_quoted_executor(executor)}" in source
    assert "IF NOT EXISTS" not in source
    assert "GRANT USAGE, CREATE ON SCHEMA" not in source
    assert "GRANT SELECT ON TABLE public.jobs TO {AGGREGATE_ROLE}" in source
    assert "SET search_path = pg_catalog" in source
    assert "WITH INHERIT FALSE, SET TRUE, ADMIN TRUE" in source
    assert "WITH ADMIN TRUE" not in source
    assert "REVOKE {AGGREGATE_ROLE} FROM" in source
    assert "REVOKE ALL ON FUNCTION public.admin_queue_depth() FROM PUBLIC" in source
    assert "REVOKE ALL ON FUNCTION public.admin_queue_depth() FROM {WORKER_ROLE}" in source
    function_setup = source[source.index("def _create_aggregate_function(") :]
    function_setup = function_setup[: function_setup.index("def _grant_aggregate_access()")]
    assert (
        function_setup.index("CREATE FUNCTION public.admin_queue_depth()")
        < function_setup.index("REVOKE ALL ON FUNCTION public.admin_queue_depth() FROM PUBLIC")
        < function_setup.index("GRANT EXECUTE ON FUNCTION public.admin_queue_depth() TO {APP_ROLE}")
        < function_setup.index("GRANT CREATE ON SCHEMA public")
        < function_setup.index("ALTER FUNCTION public.admin_queue_depth() OWNER TO")
        < function_setup.index("_revoke_temporary_membership(executor)")
        < function_setup.index("REVOKE CREATE ON SCHEMA public")
        < function_setup.index("_assert_role_has_no_memberships()")
    )



def test_queue_aggregate_downgrade_temporarily_sets_role_to_drop_owned_function():
    """Remove downgrade membership immediately after dropping the owned function."""
    source = (MIGRATIONS_DIR / "0012_admin_queue_aggregate.py").read_text()
    downgrade = source[source.index("def downgrade()") :]
    assert (
        downgrade.index("_require_downgrade_superuser()")
        < downgrade.index("_assert_role_has_no_memberships()")
        < downgrade.index("_grant_temporary_membership(executor)")
        < downgrade.index("_assert_temporary_membership(executor, admin_option=False)")
    )
    assert (
        downgrade.index("_assert_temporary_membership(executor, admin_option=False)")
        < downgrade.index("SET ROLE {AGGREGATE_ROLE}")
        < downgrade.index("DROP FUNCTION public.admin_queue_depth()")
    )
    reset_role = downgrade.index("RESET ROLE")
    revoke_membership = downgrade.index("_revoke_temporary_membership(executor)", reset_role)
    assert (
        reset_role
        < revoke_membership
        < downgrade.index("_assert_role_has_no_memberships()", revoke_membership)
        < downgrade.index("DROP ROLE {AGGREGATE_ROLE}")
    )

@pytest.fixture
def isolated_worker_database(monkeypatch):
    """Require distinct roles and credentials for a database named as a test DB."""
    api_value = os.environ.get("JARVIS_RLS_TEST_DATABASE_URL")
    worker_value = os.environ.get("JARVIS_RLS_TEST_WORKER_DATABASE_URL")
    if not api_value or not worker_value:
        pytest.skip("set both JARVIS_RLS_TEST_*_DATABASE_URL values for isolated RLS tests")
    api_url = make_url(api_value)
    worker_url = make_url(worker_value)
    if not api_url.database or not api_url.database.endswith("_test"):
        pytest.fail("RLS integration tests require an isolated database name ending in _test")
    if (
        api_url.database != worker_url.database
        or api_url.host != worker_url.host
        or api_url.port != worker_url.port
    ):
        pytest.fail("API and worker test URLs must target the same isolated test database")
    if api_url.username != "jarvis_app" or worker_url.username != "jarvis_worker":
        pytest.fail("RLS test URLs must authenticate as jarvis_app and jarvis_worker")
    if not api_url.password or not worker_url.password or api_url.password == worker_url.password:
        pytest.fail("API and worker test URLs must use distinct non-empty passwords")
    monkeypatch.setattr(settings, "database_url", api_value)
    monkeypatch.setattr(settings, "worker_database_url", worker_value)



async def _create_foreign_job() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create an API-owned task and job that the other tenant cannot see."""
    user_id, task_id = uuid.uuid4(), uuid.uuid4()
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        session.add(
            User(
                id=user_id,
                email=f"{user_id}@example.com",
                password_hash="test-only",
                display_name="RLS test",
            )
        )
        task = Task(
            id=task_id,
            user_id=user_id,
            title="RLS test",
            instruction="test only",
            status="created",
        )
        session.add(task)
        await session.flush()
        created_job = await insert_job(session, user_id=user_id, task_id=task_id)
        await session.commit()
    return user_id, task_id, created_job



@pytest.mark.asyncio
async def test_api_role_cannot_gain_worker_bypass_from_custom_guc(isolated_worker_database):
    """The API role cannot self-assert worker access with a custom GUC or SET ROLE."""
    async with session_scope() as session:
        baseline = (
            await session.execute(text("SELECT * FROM public.admin_queue_depth()"))
        ).mappings().one()
    _, _, job_id = await _create_foreign_job()
    async with session_scope() as session:
        await set_tenant_guc(session, uuid.uuid4())
        await session.execute(text("SELECT set_config('app.worker', 'true', true)"))
        row = (
            await session.execute(
                text("SELECT id FROM jobs WHERE id = :id"), {"id": job_id}
            )
        ).one_or_none()
        assert row is None
        counts = (
            await session.execute(text("SELECT * FROM public.admin_queue_depth()"))
        ).mappings().one()
        assert counts["queued"] == baseline["queued"] + 1
        with pytest.raises(DBAPIError):
            await session.execute(text("SET ROLE jarvis_worker"))
        await session.rollback()



@pytest.mark.asyncio
async def test_worker_role_can_access_queue_but_not_other_tenant_tables(isolated_worker_database):
    """Worker identity bypasses queue RLS only; ordinary tenant RLS still applies."""
    _, task_id, job_id = await _create_foreign_job()
    async with worker_session_scope() as session:
        assert await session.scalar(text("SELECT session_user")) == "jarvis_worker"
        job = (
            await session.execute(
                text("SELECT id FROM jobs WHERE id = :id"), {"id": job_id}
            )
        ).one_or_none()
        assert job is not None
        task_count = await session.scalar(
            text("SELECT count(*) FROM tasks WHERE id = :id"), {"id": task_id}
        )
        assert task_count == 0
        can_read_idempotency = await session.scalar(
            text(
                "SELECT has_table_privilege(session_user, 'public.idempotency_keys', 'SELECT')"
            )
        )
        assert can_read_idempotency is False
        with pytest.raises(DBAPIError):
            await session.execute(text("SELECT id FROM users LIMIT 1"))
        await session.rollback()
        with pytest.raises(DBAPIError):
            await session.execute(text("SELECT * FROM public.admin_queue_depth()"))
        await session.rollback()


def test_worker_session_scope_fails_closed_without_worker_url(monkeypatch):
    """Worker connections never fall back to the API database credential."""
    monkeypatch.setattr(settings, "worker_database_url", "")
    with pytest.raises(RuntimeError, match="WORKER_DATABASE_URL"):
        worker_session_scope()
