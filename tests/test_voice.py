"""Phase 7 voice. Browser capture is not claimed; these cover the contract.

Speech providers are unconfigured in this environment. The API must say so
instead of inventing a transcript, and a spoken reply must stay short.
"""

import uuid

import pytest
from jarvis_shared.speech import check_audio, normalize_locale, speech_language


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
