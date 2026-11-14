"""OAuth connections. Tokens are ciphertext, and a missing connection is not queued.

These do not call Google. A successful exchange is injected. A refused exchange
is a client error, not "not configured".
"""

import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from app.config import settings
from app.db import session_scope
from app.integrations.crypto import open_tokens, seal
from cryptography.fernet import Fernet
from sqlalchemy import text


async def _register(client, name: str) -> dict:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": f"{name}-{uuid.uuid4()}@example.com",
            "password": "correct-horse",
            "display_name": name,
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def google_ready(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")
    monkeypatch.setattr(settings, "google_redirect_uri", "http://localhost:3000/")
    monkeypatch.setattr(settings, "fernet_key", Fernet.generate_key().decode())


@pytest.mark.asyncio
async def test_connect_refuses_when_unconfigured(client, monkeypatch):
    headers = await _register(client, "none")
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")
    response = await client.post("/api/v1/integrations/gmail/connect", headers=headers)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "integration_not_configured"


@pytest.mark.asyncio
async def test_connect_url_uses_pkce_and_hides_the_secret(client, google_ready):
    headers = await _register(client, "pkce")
    response = await client.post("/api/v1/integrations/gmail/connect", headers=headers)
    assert response.status_code == 200, response.text
    url = response.json()["authorize_url"]
    query = parse_qs(urlparse(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    assert query["redirect_uri"] == ["http://localhost:3000/"]
    assert "gmail.readonly" in query["scope"][0]
    assert "client-secret" not in url


@pytest.mark.asyncio
async def test_callback_stores_ciphertext_and_blocks_other_users(client, google_ready, monkeypatch):
    owner = await _register(client, "owner")
    other = await _register(client, "other")
    started = await client.post("/api/v1/integrations/gmail/connect", headers=owner)
    state = parse_qs(urlparse(started.json()["authorize_url"]).query)["state"][0]

    foreign = await client.post(
        "/api/v1/integrations/google/callback",
        headers=other,
        json={"code": "abc", "state": state},
    )
    assert foreign.status_code == 400

    async def fake_exchange(code: str, verifier: str) -> dict:
        assert code == "abc"
        assert verifier
        return {"access_token": "ya29-secret", "refresh_token": "1//secret", "expires_in": 3600}

    monkeypatch.setattr("app.routers.integrations.google.exchange_code", fake_exchange)
    done = await client.post(
        "/api/v1/integrations/google/callback",
        headers=owner,
        json={"code": "abc", "state": state},
    )
    assert done.status_code == 200, done.text

    listed = await client.get("/api/v1/integrations", headers=owner)
    body = listed.json()["integrations"]
    gmail = next(item for item in body if item["provider"] == "gmail")
    assert gmail["connection"]["connected"] is True
    assert "ya29-secret" not in listed.text

    async with session_scope() as session:
        row = (
            await session.execute(
                text(
                    """
                    select i.token_ciphertext
                    from integrations i
                    join oauth_states s on s.user_id = i.user_id and s.provider = i.provider
                    where s.state = :state
                    """
                ),
                {"state": state},
            )
        ).one()
    assert "ya29-secret" not in row.token_ciphertext
    opened = open_tokens(row.token_ciphertext)
    assert opened["access_token"] == "ya29-secret"

    again = await client.post(
        "/api/v1/integrations/google/callback",
        headers=owner,
        json={"code": "abc", "state": state},
    )
    assert again.status_code == 400


@pytest.mark.asyncio
async def test_refused_code_is_not_reported_as_unconfigured(client, google_ready, monkeypatch):
    headers = await _register(client, "refused")
    started = await client.post("/api/v1/integrations/gmail/connect", headers=headers)
    state = parse_qs(urlparse(started.json()["authorize_url"]).query)["state"][0]

    async def refuse(code: str, verifier: str) -> dict:
        from app.ai.base import ProviderError

        raise ProviderError("Google refused the token request.")

    monkeypatch.setattr("app.routers.integrations.google.exchange_code", refuse)
    response = await client.post(
        "/api/v1/integrations/google/callback",
        headers=headers,
        json={"code": "bad", "state": state},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "token_exchange_failed"


@pytest.mark.asyncio
async def test_email_command_is_not_queued_without_a_connection(client):
    headers = await _register(client, "mail")
    submitted = await client.post(
        "/api/v1/assistant/commands",
        headers=headers,
        json={"command": "email summarize the inbox", "source": "web"},
    )
    assert submitted.status_code == 202, submitted.text
    body = submitted.json()
    assert body["task_id"] is None
    assert "gmail" in body["response_text"]

    created = await client.post(
        "/api/v1/tasks", headers=headers, json={"instruction": "email summarize the inbox"}
    )
    assert created.status_code == 409
    assert created.json()["error"]["code"] == "integration_required"


@pytest.mark.asyncio
async def test_disconnect_deletes_ciphertext(client, google_ready):
    headers = await _register(client, "drop")
    started = await client.post("/api/v1/integrations/google_calendar/connect", headers=headers)
    assert started.status_code == 200
    ciphertext = seal({"access_token": "temp"})
    async with session_scope() as session:
        user_id = (
            await session.execute(text("select user_id from oauth_states order by created_at desc limit 1"))
        ).scalar_one()
        await session.execute(
            text(
                """
                insert into integrations (id, user_id, provider, status, scopes, token_ciphertext, connected_at)
                values (:id, :user_id, 'google_calendar', 'connected', '{}', :cipher, now())
                """
            ),
            {"id": uuid.uuid4(), "user_id": user_id, "cipher": ciphertext},
        )
        await session.commit()

    gone = await client.delete("/api/v1/integrations/google_calendar", headers=headers)
    assert gone.status_code == 200, gone.text
    async with session_scope() as session:
        row = (
            await session.execute(
                text("select status, token_ciphertext from integrations where user_id = :id"),
                {"id": user_id},
            )
        ).one()
    assert row.status == "revoked"
    assert row.token_ciphertext is None
