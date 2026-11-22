"""Shared fixtures with a fail-closed guard for database-backed tests."""

from urllib.parse import unquote, urlsplit

import pytest
from app.config import settings
from app.main import app
from app.routers.auth import reset_throttle
from httpx import ASGITransport, AsyncClient

_DATABASE_SESSION_NAMES = frozenset(
    {"get_session", "session_scope", "worker_session_scope"}
)


def _database_name_from_url(
    database_url: str | None, setting_name: str = "DATABASE_URL"
) -> str:
    """Return a PostgreSQL test database name without exposing URL details."""
    generic_error = (
        f"{setting_name} must be a valid PostgreSQL URL with an explicit database name"
    )
    if not database_url:
        raise ValueError(f"{setting_name} is required for database-backed tests")
    try:
        parsed = urlsplit(database_url)
        database_name = unquote(parsed.path[1:]) if parsed.path.startswith("/") else ""
    except ValueError:
        raise ValueError(generic_error) from None

    is_postgresql = parsed.scheme in {"postgres", "postgresql"} or (
        parsed.scheme.startswith("postgresql+")
        and len(parsed.scheme) > len("postgresql+")
    )
    if not is_postgresql or not database_name or "/" in database_name:
        raise ValueError(generic_error)
    if not database_name.endswith("_test"):
        raise ValueError(
            f"{setting_name} must target a database name ending in '_test'; "
            "refusing database-backed tests"
        )
    return database_name


def _require_explicit_test_database(
    database_url: str | None, setting_name: str, is_explicit: bool
) -> str:
    """Reject application defaults and validate an explicitly set database URL."""
    if not is_explicit:
        raise ValueError(
            f"{setting_name} must be explicitly set to a PostgreSQL test database "
            "ending in '_test'; refusing database-backed tests"
        )
    return _database_name_from_url(database_url, setting_name)


def _database_session_scopes(function, visited=None):
    """Find direct database-session calls made by a test or its local helpers."""
    function = getattr(function, "func", function)
    code = getattr(function, "__code__", None)
    if code is None:
        return set()
    visited = set() if visited is None else visited
    if id(function) in visited:
        return set()
    visited.add(id(function))
    scopes = set(_DATABASE_SESSION_NAMES.intersection(code.co_names))
    globals_ = function.__globals__
    for name in code.co_names:
        referenced = globals_.get(name)
        if callable(referenced):
            scopes.update(_database_session_scopes(referenced, visited))
    return scopes


def _database_scopes_for_item(item):
    """Identify tests that need an explicitly configured database target."""
    scopes = _database_session_scopes(getattr(item, "function", None))
    module_globals = vars(item.module)
    for fixture_name in item.fixturenames:
        scopes.update(_database_session_scopes(module_globals.get(fixture_name)))
    if "client" in item.fixturenames:
        scopes.add("session_scope")
    return scopes


def pytest_runtest_setup(item):
    """Check database targets before pytest sets up any test fixtures."""
    scopes = _database_scopes_for_item(item)
    if not scopes:
        return
    explicit_fields = settings.model_fields_set
    try:
        if scopes.intersection({"get_session", "session_scope"}):
            _require_explicit_test_database(
                settings.database_url,
                "DATABASE_URL",
                "database_url" in explicit_fields,
            )
        if "worker_session_scope" in scopes:
            _require_explicit_test_database(
                settings.worker_database_url,
                "WORKER_DATABASE_URL",
                "worker_database_url" in explicit_fields,
            )
    except ValueError as error:
        pytest.fail(str(error), pytrace=False)


@pytest.fixture(autouse=True)
def _reset_auth_throttle():
    """Clear the in-process login/register throttle before each test.

    Tests share one client IP (ASGITransport has no real peer), so without
    this reset, one test's register/login calls would exhaust the budget
    for every test that runs after it in the same process.
    """
    reset_throttle()
    yield


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
