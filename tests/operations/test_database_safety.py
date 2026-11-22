"""Database URL guard tests that do not connect to PostgreSQL."""

import pytest
from conftest import _database_name_from_url, _require_explicit_test_database


def test_accepts_explicit_test_database_url():
    url = "postgresql+asyncpg://fake_user:test-secret-marker@db:5432/jarvis_test"

    assert _database_name_from_url(url) == "jarvis_test"


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "not-a-database-url",
        "postgresql+asyncpg://fake_user:test-secret-marker@db:5432/jarvis",
        "postgresql+asyncpg://fake_user:test-secret-marker@db:5432/jarvis_test_backup",
        "postgresql+asyncpg://fake_user:test-secret-marker@[invalid",
        "https://fake_user:test-secret-marker@db/jarvis_test",
    ],
    ids=[
        "missing-none",
        "missing-empty",
        "malformed",
        "development-database",
        "test-suffix-mismatch",
        "malformed-authority",
        "non-postgresql-scheme",
    ],
)
def test_rejects_missing_invalid_or_non_test_database_urls(url):
    with pytest.raises(ValueError) as error:
        _database_name_from_url(url)

    assert "fake_user" not in str(error.value)
    assert "test-secret-marker" not in str(error.value)


def test_rejects_implicit_application_database_fallback():
    url = "postgresql+asyncpg://fake_user:test-secret-marker@db:5432/jarvis_test"

    with pytest.raises(ValueError, match="explicitly set") as error:
        _require_explicit_test_database(url, "DATABASE_URL", is_explicit=False)

    assert "fake_user" not in str(error.value)
    assert "test-secret-marker" not in str(error.value)