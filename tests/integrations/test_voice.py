"""Phase 7 voice. Browser capture is not claimed; these cover the contract.

Speech providers are unconfigured in this environment. The API must say so
instead of inventing a transcript, and a spoken reply must stay short.
"""

import uuid

import jwt
import pytest
from app.ai.base import ProviderError
from app.config import settings
from app.db import session_scope, set_tenant_guc
from app.integrations.runner import IntegrationStepError, run_step
from app.models import Job, Task, TaskStep
from jarvis_shared.speech import check_audio, normalize_locale, speech_language
from sqlalchemy import select


def test_locale_rules():
    assert normalize_locale("en-IN") == "en"
    assert normalize_locale("HI") == "hi"
    assert speech_language("hi") == "hi-IN"
    assert speech_language("en-US") == "en-US"
    with pytest.raises(ValueError):
        normalize_locale("fr")
    with pytest.raises(ValueError):
        check_audio("text/plain", 10)
    with pytest.raises(ValueError):
        check_audio("audio/webm", 0)
    check_audio("audio/webm;codecs=opus", 32)


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


@pytest.mark.asyncio
async def test_voice_session_requires_auth_and_rejects_unknown_locale(client):
    missing = await client.get("/api/v1/voice/session")
    assert missing.status_code == 401

    headers = await _register(client, "voice")
    session = await client.get("/api/v1/voice/session", headers=headers, params={"locale": "hi-IN"})
    assert session.status_code == 200, session.text
    body = session.json()
    assert body["locale"] == "hi"
    assert body["language"] == "hi-IN"
    assert body["push_to_talk"] is True
    assert body["wake_word"] is False
    assert body["stt_configured"] is False
    assert body["text_fallback"] is True

    rejected = await client.get("/api/v1/voice/session", headers=headers, params={"locale": "fr"})
    assert rejected.status_code == 422


@pytest.mark.asyncio
async def test_speak_shortens_and_does_not_invent_audio(client):
    headers = await _register(client, "speak")
    long = "word " * 80
    spoken = await client.post(
        "/api/v1/voice/speak", headers=headers, json={"text": long, "locale": "en"}
    )
    assert spoken.status_code == 200, spoken.text
    body = spoken.json()
    assert body["audio"] is None
    assert len(body["text"]) <= 240
    assert body["truncated"] is True
    assert body["language"] == "en-US"


@pytest.mark.asyncio
async def test_health_reports_speech_unconfigured(client):
    health = await client.get("/api/v1/health")
    assert health.status_code == 200
    speech = health.json()["speech"]
    assert speech["stt_configured"] is False
    assert speech["tts_configured"] is False
    assert speech["locales"] == ["en", "hi"]
    assert speech["browser_capture"] is True


@pytest.mark.asyncio
async def test_voice_command_stays_on_the_owners_conversation(client):
    owner = await _register(client, "owner")
    other = await _register(client, "other")
    created = await client.post("/api/v1/conversations", headers=owner, json={"title": "Voice"})
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    stolen = await client.post(
        "/api/v1/assistant/commands",
        headers=other,
        json={
            "command": "research compare the two libraries",
            "source": "web",
            "input_type": "voice",
            "locale": "en",
            "conversation_id": conversation_id,
        },
    )
    assert stolen.status_code == 404

    rejected = await client.post(
        "/api/v1/assistant/commands",
        headers=owner,
        json={
            "command": "research compare the two libraries",
            "source": "web",
            "input_type": "voice",
            "locale": "fr",
            "conversation_id": conversation_id,
        },
    )
    assert rejected.status_code == 422

    submitted = await client.post(
        "/api/v1/assistant/commands",
        headers=owner,
        json={
            "command": "research compare the two libraries",
            "source": "web",
            "input_type": "voice",
            "locale": "hi-IN",
            "conversation_id": conversation_id,
        },
    )
    assert submitted.status_code == 202, submitted.text
    body = submitted.json()
    assert body["conversation_id"] == conversation_id
    assert body["source"] == "web"
    assert len(body["voice_response"]) <= 240

    stored = await client.get(f"/api/v1/conversations/{conversation_id}/messages", headers=owner)
    contents = [message["content"] for message in stored.json()["messages"]]
    assert "research compare the two libraries" in contents
    assert body["response_text"] in contents


@pytest.mark.asyncio
async def test_transcribe_requires_a_configured_provider(client):
    headers = await _register(client, "transcribe")
    response = await client.post(
        "/api/v1/voice/transcribe",
        headers=headers,
        files={"file": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
        data={"locale": "en"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "stt_not_configured"


@pytest.mark.asyncio
async def test_transcribe_rejects_an_unsupported_upload(client):
    headers = await _register(client, "transcribe-bad")
    wrong_type = await client.post(
        "/api/v1/voice/transcribe",
        headers=headers,
        files={"file": ("clip.txt", b"not audio", "text/plain")},
        data={"locale": "en"},
    )
    assert wrong_type.status_code == 422
    assert wrong_type.json()["error"]["code"] == "invalid_audio"

    empty = await client.post(
        "/api/v1/voice/transcribe",
        headers=headers,
        files={"file": ("clip.webm", b"", "audio/webm")},
        data={"locale": "en"},
    )
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "invalid_audio"

    bad_locale = await client.post(
        "/api/v1/voice/transcribe",
        headers=headers,
        files={"file": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
        data={"locale": "fr"},
    )
    assert bad_locale.status_code == 422
    assert bad_locale.json()["error"]["code"] == "unsupported_locale"


@pytest.mark.asyncio
async def test_transcribe_returns_text_from_a_configured_provider(client, monkeypatch):
    class _StubTranscriber:
        name = "openai"

        async def transcribe(self, audio, *, locale, content_type):
            assert locale == "hi"
            assert content_type == "audio/webm"
            return "namaste jarvis"

    monkeypatch.setattr("app.routers.voice.get_transcriber", lambda: _StubTranscriber())

    headers = await _register(client, "transcribe-ok")
    response = await client.post(
        "/api/v1/voice/transcribe",
        headers=headers,
        files={"file": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
        data={"locale": "hi-IN"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["locale"] == "hi"
    assert body["language"] == "hi-IN"
    assert body["text"] == "namaste jarvis"


@pytest.mark.asyncio
async def test_transcribe_reports_a_provider_failure_as_503_not_500(client, monkeypatch):
    class _FailingTranscriber:
        name = "openai"

        async def transcribe(self, audio, *, locale, content_type):
            raise ProviderError("The provider rejected the request.")

    monkeypatch.setattr("app.routers.voice.get_transcriber", lambda: _FailingTranscriber())

    headers = await _register(client, "transcribe-fail")
    response = await client.post(
        "/api/v1/voice/transcribe",
        headers=headers,
        files={"file": ("clip.webm", b"fake-audio-bytes", "audio/webm")},
        data={"locale": "en"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_error"


@pytest.mark.asyncio
async def test_speak_returns_audio_when_a_tts_provider_is_configured(client, monkeypatch):
    class _StubSynthesizer:
        name = "openai"

        async def synthesize(self, text, *, locale):
            assert locale == "en"
            return b"fake-mp3-bytes"

    monkeypatch.setattr("app.routers.voice.get_synthesizer", lambda: _StubSynthesizer())

    headers = await _register(client, "speak-ok")
    response = await client.post(
        "/api/v1/voice/speak", headers=headers, json={"text": "hello there", "locale": "en"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["audio_format"] == "mp3"
    import base64

    assert base64.b64decode(body["audio"]) == b"fake-mp3-bytes"


@pytest.mark.asyncio
async def test_speak_reports_a_tts_provider_failure_as_503_not_500(client, monkeypatch):
    class _FailingSynthesizer:
        name = "openai"

        async def synthesize(self, text, *, locale):
            raise ProviderError("The provider rejected the request.")

    monkeypatch.setattr("app.routers.voice.get_synthesizer", lambda: _FailingSynthesizer())

    headers = await _register(client, "speak-fail")
    response = await client.post(
        "/api/v1/voice/speak", headers=headers, json={"text": "hello there", "locale": "en"}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_error"


class _FakeSecret:
    def __init__(self, value: str, expires_at: int) -> None:
        self.value = value
        self.expires_at = expires_at


class _FakeClientSecrets:
    def __init__(self, secret: _FakeSecret) -> None:
        self.secret = secret
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.secret


class _FakeRealtime:
    def __init__(self, secrets: _FakeClientSecrets) -> None:
        self.client_secrets = secrets


class _FakeOpenAI:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.realtime = _FakeRealtime(_SECRETS)


_SECRETS = _FakeClientSecrets(_FakeSecret("ek_short_lived_secret", 1_700_000_600))


async def _conversation(client, headers) -> str:
    created = await client.post("/api/v1/conversations", headers=headers, json={"title": "Voice"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


@pytest.mark.asyncio
async def test_realtime_session_requires_a_user(client):
    response = await client.post(
        "/api/v1/voice/realtime/session",
        json={"conversation_id": str(uuid.uuid4()), "locale": "en"},
    )
    assert response.status_code == 401
    assert "client_secret" not in response.json()



@pytest.mark.asyncio
async def test_realtime_session_mints_a_short_lived_secret_for_an_owned_conversation(
    client, monkeypatch
):
    monkeypatch.setattr("app.routers.voice.settings.openai_api_key", "sk-test-not-returned")
    monkeypatch.setattr("app.routers.voice.AsyncOpenAI", _FakeOpenAI)

    headers = await _register(client, "realtime")
    conversation_id = await _conversation(client, headers)
    response = await client.post(
        "/api/v1/voice/realtime/session",
        headers=headers,
        json={"conversation_id": conversation_id, "locale": "hi-IN"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "openai"
    assert body["transport"] == "webrtc"
    assert body["model"] == "gpt-realtime"
    assert body["conversation_id"] == conversation_id
    assert body["locale"] == "hi"
    assert body["language"] == "hi-IN"
    assert body["client_secret"] == "ek_short_lived_secret"
    assert body["expires_at"] == 1_700_000_600
    assert "sk-test-not-returned" not in response.text
    assert settings.resolved_api_key not in response.text

    sent = _SECRETS.kwargs
    assert sent["expires_after"] == {"anchor": "created_at", "seconds": 600}
    tool = sent["session"]["tools"][0][0]
    assert tool["name"] == "submit_command"
    assert set(tool["parameters"]["required"]) == {"command", "idempotency_key"}


@pytest.mark.asyncio
async def test_realtime_session_refuses_a_foreign_conversation_and_an_unconfigured_key(
    client, monkeypatch
):
    owner = await _register(client, "realtime-owner")
    other = await _register(client, "realtime-other")
    conversation_id = await _conversation(client, owner)

    stolen = await client.post(
        "/api/v1/voice/realtime/session",
        headers=other,
        json={"conversation_id": conversation_id, "locale": "en"},
    )
    assert stolen.status_code == 404
    assert stolen.json()["error"]["code"] == "not_found"

    monkeypatch.setattr("app.routers.voice.settings.openai_api_key", "")
    missing = await client.post(
        "/api/v1/voice/realtime/session",
        headers=owner,
        json={"conversation_id": conversation_id, "locale": "en"},
    )
    assert missing.status_code == 503
    assert missing.json()["error"]["code"] == "speech_not_configured"
    assert "client_secret" not in missing.json()

    rejected = await client.post(
        "/api/v1/voice/realtime/session",
        headers=owner,
        json={"conversation_id": conversation_id, "locale": "fr"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "unsupported_locale"


@pytest.mark.asyncio
async def test_realtime_tool_rejects_unsupported_research_and_holds_high_risk_work(client, monkeypatch):
    headers = await _register(client, "realtime-tool")
    conversation_id = await _conversation(client, headers)
    monkeypatch.setattr("app.routers.assistant.get_model", lambda: None)
    monkeypatch.setattr("app.routers.tasks.get_model", lambda: None)

    rejected = await client.post(
        "/api/v1/voice/realtime/tool",
        headers=headers,
        json={
            "conversation_id": conversation_id,
            "command": "research compare the two libraries",
            "idempotency_key": f"voice-research-{uuid.uuid4()}",
            "locale": "en",
        },
    )
    assert rejected.status_code == 202, rejected.text
    body = rejected.json()
    assert body["source"] == "web"
    assert body["task_id"] is None
    assert body["response_text"] == (
        "Research is unavailable because no web-search provider is implemented. "
        "I did not create a task."
    )

    unsupported_task = await client.post(
        "/api/v1/tasks",
        headers=headers,
        json={"instruction": "research compare the two libraries"},
    )
    assert unsupported_task.status_code == 422
    assert unsupported_task.json()["error"]["code"] == "not_executable"

    listed = await client.get("/api/v1/tasks", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["tasks"] == []

    held = await client.post(
        "/api/v1/voice/realtime/tool",
        headers=headers,
        json={
            "conversation_id": conversation_id,
            "command": "email send the weekly report",
            "idempotency_key": f"voice-hold-{uuid.uuid4()}",
            "locale": "en",
        },
    )
    assert held.status_code == 202, held.text
    body = held.json()
    assert body["status"] == "waiting_for_approval"
    task_id = uuid.UUID(body["task_id"])

    async with session_scope() as session:
        user_id = uuid.UUID(jwt.decode(headers["Authorization"].removeprefix("Bearer "), options={"verify_signature": False})["sub"])
        await set_tenant_guc(session, user_id)
        task = await session.get(Task, task_id)
        jobs = (await session.execute(select(Job).where(Job.task_id == task_id))).scalars().all()
    assert task.status == "waiting_for_approval"
    assert jobs == []


@pytest.mark.asyncio
async def test_runner_fails_an_agent_with_no_provider_instead_of_inventing_a_result():
    step = TaskStep(task_id=uuid.uuid4(), position=0, agent_id="research", action="look", risk="low")
    with pytest.raises(IntegrationStepError) as raised:
        await run_step(uuid.uuid4(), step)
    assert str(raised.value) == "agent_not_implemented"
