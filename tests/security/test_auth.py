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


@pytest.mark.asyncio
async def test_preferences_are_read_and_updated_for_the_authenticated_user(client):
    first = await _register(client)
    second = await _register(client)
    first_headers = {"Authorization": f"Bearer {first.json()['access_token']}"}
    second_headers = {"Authorization": f"Bearer {second.json()['access_token']}"}

    initial = await client.get("/api/v1/auth/preferences", headers=first_headers)
    assert initial.status_code == 200
    assert initial.json()["preferences"] == {"timezone": "UTC", "briefing_language": "en"}

    updated = await client.patch(
        "/api/v1/auth/preferences",
        headers=first_headers,
        json={"timezone": "America/Los_Angeles", "briefing_language": "hi"},
    )
    assert updated.status_code == 200
    assert updated.json()["preferences"] == {
        "timezone": "America/Los_Angeles",
        "briefing_language": "hi",
    }

    other_user_preferences = await client.get(
        "/api/v1/auth/preferences", headers=second_headers
    )
    assert other_user_preferences.status_code == 200
    assert other_user_preferences.json()["preferences"] == {
        "timezone": "UTC",
        "briefing_language": "en",
    }

    invalid = await client.patch(
        "/api/v1/auth/preferences",
        headers=first_headers,
        json={"briefing_language": "fr"},
    )
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_session_listing_revocation_ownership_and_repeated_calls(client):
    password = "test-only-session-password-123"
    first = await _register(client, password=password)
    first_token = first.json()["access_token"]
    first_headers = {"Authorization": f"Bearer {first_token}"}
    another_session = await client.post(
        "/api/v1/auth/login",
        json={"email": first.json()["user"]["email"], "password": password},
    )
    assert another_session.status_code == 200

    listed = await client.get("/api/v1/auth/sessions", headers=first_headers)
    assert listed.status_code == 200
    sessions = listed.json()["sessions"]
    assert len(sessions) == 2
    assert sum(item["current"] for item in sessions) == 1
    current = next(item for item in sessions if item["current"])
    other_session = next(item for item in sessions if not item["current"])

    revoked = await client.delete(
        f"/api/v1/auth/sessions/{other_session['id']}", headers=first_headers
    )
    repeated = await client.delete(
        f"/api/v1/auth/sessions/{other_session['id']}", headers=first_headers
    )
    assert revoked.status_code == 200
    assert repeated.status_code == 200
    remaining = await client.get("/api/v1/auth/sessions", headers=first_headers)
    assert [item["id"] for item in remaining.json()["sessions"]] == [current["id"]]

    second_user = await _register(client)
    second_headers = {"Authorization": f"Bearer {second_user.json()['access_token']}"}
    second_sessions = await client.get(
        "/api/v1/auth/sessions", headers=second_headers
    )
    foreign_id = second_sessions.json()["sessions"][0]["id"]
    foreign_revoke = await client.delete(
        f"/api/v1/auth/sessions/{foreign_id}", headers=first_headers
    )
    assert foreign_revoke.status_code == 404
    assert (await client.get("/api/v1/auth/session", headers=second_headers)).status_code == 200


@pytest.mark.asyncio
async def test_logout_only_revokes_the_current_session(client):
    password = "test-only-logout-password-123"
    registered = await _register(client, password=password)
    earlier_headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    logged_in = await client.post(
        "/api/v1/auth/login",
        json={"email": registered.json()["user"]["email"], "password": password},
    )
    current_headers = {"Authorization": f"Bearer {logged_in.json()['access_token']}"}

    logout = await client.post("/api/v1/auth/logout", headers=current_headers)
    assert logout.status_code == 200
    assert (await client.get("/api/v1/auth/session", headers=current_headers)).status_code == 401
    assert (await client.get("/api/v1/auth/session", headers=earlier_headers)).status_code == 200
