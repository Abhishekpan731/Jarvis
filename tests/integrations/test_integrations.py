"""OAuth connections. Tokens are ciphertext, and a missing connection is not queued.

These do not call Google. A successful exchange is injected. A refused exchange
is a client error, not "not configured".
"""

import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from app.config import settings
from app.db import session_scope, set_tenant_guc
from app.integrations.crypto import open_tokens, seal
from app.models import Integration
from app.routers import integrations
from cryptography.fernet import Fernet
from fastapi import HTTPException
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


async def _user_id(client, headers: dict) -> uuid.UUID:
    me = await client.get("/api/v1/auth/session", headers=headers)
    assert me.status_code == 200, me.text
    return uuid.UUID(me.json()["user"]["id"])


@pytest.fixture
def google_ready(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "client-id")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret")
    monkeypatch.setattr(settings, "google_redirect_uri", "http://localhost:3000/")
    monkeypatch.setattr(settings, "fernet_key", Fernet.generate_key().decode())


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []

    def scalar_one_or_none(self):
        return None


class _ReadinessSession:
    async def execute(self, _statement):
        return _EmptyResult()

    def add(self, _row):
        raise AssertionError("unconfigured connect must not create OAuth state")

    async def commit(self):
        raise AssertionError("unconfigured connect must not persist OAuth state")


class _CallbackSession:
    def __init__(self):
        self.records = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, _statement):
        return _EmptyResult()

    def add(self, record):
        self.records.append(record)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("google_client_id", ""),
        ("google_client_secret", " "),
        ("fernet_key", ""),
        ("fernet_key", "not-a-fernet-key"),
    ],
)
@pytest.mark.asyncio
async def test_readiness_and_connect_share_prerequisites_without_database(
    google_ready, monkeypatch, setting, value
):
    monkeypatch.setattr(settings, setting, value)
    user = SimpleNamespace(id=uuid.uuid4())
    session = _ReadinessSession()

    listed = await integrations.list_integrations(user=user, session=session)
    assert all(item["configured"] is False for item in listed["integrations"])

    with pytest.raises(HTTPException) as raised:
        await integrations.connect("gmail", user=user, session=session)
    assert raised.value.status_code == 503
    assert raised.value.detail["error"]["code"] == "integration_not_configured"


@pytest.mark.asyncio
async def test_callback_rolls_back_and_retries_when_encryption_is_unavailable(
    google_ready, monkeypatch
):
    user = SimpleNamespace(id=uuid.uuid4())
    oauth_state = SimpleNamespace(provider="gmail", user_id=user.id, code_verifier="verifier")

    async def claim(_session, state, user_id):
        assert state == "state"
        assert user_id == user.id
        return oauth_state

    async def exchange(code, verifier):
        assert code == "code"
        assert verifier == "verifier"
        return {"access_token": "test-access-token"}

    monkeypatch.setattr("app.routers.integrations._claim_state", claim)
    monkeypatch.setattr("app.routers.integrations.google.exchange_code", exchange)
    valid_key = settings.fernet_key
    monkeypatch.setattr(settings, "fernet_key", "not-a-fernet-key")
    session = _CallbackSession()
    body = integrations.CallbackIn(code="code", state="state")

    with pytest.raises(HTTPException) as raised:
        await integrations.callback(body=body, user=user, session=session)
    assert raised.value.status_code == 503
    assert session.rollbacks == 1
    assert session.commits == 0
    assert session.records == []

    monkeypatch.setattr(settings, "fernet_key", valid_key)
    connected = await integrations.callback(body=body, user=user, session=session)
    assert connected == {"connected": True, "provider": "gmail"}
    integration = next(record for record in session.records if isinstance(record, Integration))
    assert "test-access-token" not in integration.token_ciphertext
    assert open_tokens(integration.token_ciphertext)["access_token"] == "test-access-token"

@pytest.mark.parametrize(
    ("credential", "value"),
    [
        ("google_client_id", ""),
        ("google_client_id", "   "),
        ("google_client_id", " client-id "),
        ("google_client_secret", ""),
        ("google_client_secret", "   "),
        ("google_client_secret", " client-secret "),
    ],
)
@pytest.mark.asyncio
async def test_readiness_rejects_blank_or_padded_google_credentials(
    client, google_ready, monkeypatch, credential, value
):
    headers = await _register(client, "missing-google-credential")
    monkeypatch.setattr(settings, credential, value)
    listed = await client.get("/api/v1/integrations", headers=headers)
    assert listed.status_code == 200, listed.text
    assert all(item["configured"] is False for item in listed.json()["integrations"])

    response = await client.post("/api/v1/integrations/gmail/connect", headers=headers)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "integration_not_configured"


@pytest.mark.parametrize("fernet_key", ["", "not-a-fernet-key"])
@pytest.mark.asyncio
async def test_readiness_requires_valid_fernet_key(
    client, google_ready, monkeypatch, fernet_key
):
    headers = await _register(client, "missing-fernet-key")
    monkeypatch.setattr(settings, "fernet_key", fernet_key)
    listed = await client.get("/api/v1/integrations", headers=headers)
    assert listed.status_code == 200, listed.text
    assert all(item["configured"] is False for item in listed.json()["integrations"])

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

    owner_id = await _user_id(client, owner)
    async with session_scope() as session:
        await set_tenant_guc(session, owner_id)
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
        audit = (
            await session.execute(
                text(
                    """
                    select action, target_type, target_id from audit_logs
                    where user_id = :user_id and action = 'integration_connect'
                    """
                ),
                {"user_id": owner_id},
            )
        ).one()
    assert tuple(audit) == ("integration_connect", "integration", "gmail")
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



@pytest.mark.parametrize("fernet_key", ["", "not-a-fernet-key"])
@pytest.mark.asyncio
async def test_callback_keeps_state_retryable_when_encryption_is_unavailable(
    client, google_ready, monkeypatch, fernet_key
):
    headers = await _register(client, "callback-fernet-error")
    started = await client.post("/api/v1/integrations/gmail/connect", headers=headers)
    state = parse_qs(urlparse(started.json()["authorize_url"]).query)["state"][0]
    valid_key = settings.fernet_key

    async def exchange(code: str, verifier: str) -> dict:
        assert code == "abc"
        assert verifier
        return {"access_token": "test-access-token"}

    monkeypatch.setattr("app.routers.integrations.google.exchange_code", exchange)
    monkeypatch.setattr(settings, "fernet_key", fernet_key)
    unavailable = await client.post(
        "/api/v1/integrations/google/callback", headers=headers, json={"code": "abc", "state": state}
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "integration_not_configured"

    monkeypatch.setattr(settings, "fernet_key", valid_key)
    retried = await client.post(
        "/api/v1/integrations/google/callback", headers=headers, json={"code": "abc", "state": state}
    )
    assert retried.status_code == 200, retried.text

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
    user_id = (
        await _user_id(client, headers)
    )
    async with session_scope() as session:
        await set_tenant_guc(session, user_id)
        found = (
            await session.execute(
                text("select user_id from oauth_states where user_id = :id order by created_at desc limit 1"),
                {"id": user_id},
            )
        ).scalar_one()
        assert found == user_id
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
        await set_tenant_guc(session, user_id)
        row = (
            await session.execute(
                text("select status, token_ciphertext from integrations where user_id = :id"),
                {"id": user_id},
            )
        ).one()
        audit = (
            await session.execute(
                text(
                    """
                    select action, target_type, target_id from audit_logs
                    where user_id = :user_id and action = 'integration_disconnect'
                    """
                ),
                {"user_id": user_id},
            )
        ).one()
    assert row.status == "revoked"
    assert row.token_ciphertext is None
    assert tuple(audit) == ("integration_disconnect", "integration", "google_calendar")
