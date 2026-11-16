"""Shared fixtures. Tests run against the real development database."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.auth import reset_throttle


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
