"""Authentication lifecycle, run against PostgreSQL."""

import uuid

import pytest


def _email() -> str:
    return f"user-{uuid.uuid4().hex[:8]}@example.com"


async def _register(client, email=None, password="correct horse battery"):
    return await client.post(
        "/api/v1/auth/register",
        json={"email": email or _email(), "password": password, "display_name": "Test"},
    )


@pytest.mark.asyncio
async def test_register_is_throttled_per_ip(client):
    # The budget (8 per 5 minutes) is shared across emails for one client IP,
    # so distinct emails still trip the limit once the budget is spent.
    for _ in range(8):
        reply = await _register(client)
        assert reply.status_code == 201

    throttled = await _register(client)
    assert throttled.status_code == 429
    assert throttled.json()["error"]["code"] == "rate_limited"


@pytest.mark.asyncio
async def test_register_login_session_and_logout(client):
    email = _email()
    created = await _register(client, email)
    assert created.status_code == 201, created.text
    token = created.json()["access_token"]
    assert "jarvis_refresh" in created.cookies
    assert created.json()["user"]["email"] == email

    session = await client.get("/api/v1/auth/session", headers={"Authorization": f"Bearer {token}"})
    assert session.status_code == 200

    logged_out = await client.post(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert logged_out.status_code == 200

    revoked = await client.get("/api/v1/auth/session", headers={"Authorization": f"Bearer {token}"})
    assert revoked.status_code == 401


@pytest.mark.asyncio
async def test_login_rejects_wrong_password_without_detail(client):
    email = _email()
    await _register(client, email)
    wrong = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "wrong password"}
    )
    missing = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong password"}
    )
    assert wrong.status_code == 401
    assert missing.status_code == 401
    assert wrong.json() == missing.json()


@pytest.mark.asyncio
async def test_refresh_rotates_and_reuse_revokes_the_family(client):
    created = await _register(client)
    first = created.cookies["jarvis_refresh"]

    refreshed = await client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200
    second = refreshed.cookies["jarvis_refresh"]
    assert second != first

    reused = await client.post("/api/v1/auth/refresh", cookies={"jarvis_refresh": first})
    assert reused.status_code == 401

    # The whole family is dead, including the token just issued.
    also_dead = await client.post("/api/v1/auth/refresh", cookies={"jarvis_refresh": second})
    assert also_dead.status_code == 401


@pytest.mark.asyncio
async def test_password_change_and_account_deletion(client):
    email = _email()
    created = await _register(client, email)
    token = created.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    changed = await client.post(
        "/api/v1/auth/password",
        headers=headers,
        json={
            "current_password": "correct horse battery",
            "new_password": "a different passphrase",
        },
    )
    assert changed.status_code == 200

    old = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct horse battery"}
    )
    assert old.status_code == 401
    new = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "a different passphrase"}
    )
    assert new.status_code == 200

    new_headers = {"Authorization": f"Bearer {new.json()['access_token']}"}
    deleted = await client.delete("/api/v1/auth/account", headers=new_headers)
    assert deleted.status_code == 200
    # The token issued before deletion dies with the account.
    assert (await client.get("/api/v1/auth/session", headers=new_headers)).status_code == 401
    gone = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "a different passphrase"}
    )
    assert gone.status_code == 401


@pytest.mark.asyncio
async def test_users_cannot_see_each_others_conversations(client):
    first = await _register(client)
    second = await _register(client)
    headers = {"Authorization": f"Bearer {first.json()['access_token']}"}
    created = await client.post("/api/v1/conversations", headers=headers, json={"title": "private"})
    conversation_id = created.json()["id"]

    other = {"Authorization": f"Bearer {second.json()['access_token']}"}
    leaked = await client.get(f"/api/v1/conversations/{conversation_id}", headers=other)
    assert leaked.status_code == 404
