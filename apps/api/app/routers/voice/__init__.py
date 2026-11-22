"""Voice session support. Browser push-to-talk capture/playback is the default
path; transcribe/speak below are an additional, opt-in server-side path that
only does anything once a speech provider credential is configured (Phase
7a). The command itself still goes through POST /api/v1/assistant/commands.
"""

import base64
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from jarvis_shared.commands import voice_reply
from jarvis_shared.speech import check_audio, normalize_locale, speech_language
from openai import APIError, AsyncOpenAI
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.base import NotConfigured, ProviderError
from app.ai.speech import get_synthesizer, get_transcriber, speech_status
from app.config import settings
from app.db import get_session
from app.limits import limit_writes_durable
from app.models import Conversation, User
from app.routers.assistant import CommandIn, submit_command
from app.security import error, require_user

router = APIRouter(prefix="/api/v1/voice", tags=["voice"])


_REALTIME_SECRET_TTL_SECONDS = 600
_SUBMIT_COMMAND_TOOL = {
    "type": "function",
    "name": "submit_command",
    "description": (
        "Send the user's spoken request to Jarvis. Call this only when the user "
        "has asked Jarvis to do something, and pass their request unchanged."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The user's request, verbatim."},
            "idempotency_key": {
                "type": "string",
                "description": "A unique key for this request, reused on a retry.",
            },
        },
        "required": ["command", "idempotency_key"],
        "additionalProperties": False,
    },
}


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    locale: str = Field(default="en", max_length=16)


class RealtimeSessionIn(BaseModel):
    conversation_id: uuid.UUID
    locale: str = Field(default="en", max_length=16)


class RealtimeToolIn(BaseModel):
    conversation_id: uuid.UUID
    command: str = Field(min_length=1, max_length=20_000)
    idempotency_key: str = Field(min_length=8, max_length=200)
    locale: str = Field(default="en", max_length=16)


def _locale_or_422(raw: str) -> str:
    try:
        return normalize_locale(raw)
    except ValueError:
        raise HTTPException(
            422, error("unsupported_locale", "Voice supports English (en) and Hindi (hi).")
        ) from None


@router.get("/session")
async def session(user: User = Depends(require_user), locale: str = "en") -> dict:
    base = _locale_or_422(locale)
    status = speech_status()
    return {
        "locale": base,
        "language": speech_language(base),
        "push_to_talk": True,
        "wake_word": False,
        "text_fallback": True,
        **status,
    }


@router.post("/speak")
async def speak(
    body: SpeakIn,
    user: User = Depends(require_user),
) -> dict:
    """Return the text the browser should speak, and audio if a TTS provider
    is configured. Without one, audio is None and the browser speaks the text
    itself; that is not an error.
    """
    base = _locale_or_422(body.locale)
    spoken = voice_reply(body.text)
    audio: str | None = None
    audio_format: str | None = None
    try:
        synthesizer = get_synthesizer()
    except NotConfigured:
        synthesizer = None
    if synthesizer is not None:
        try:
            clip = await synthesizer.synthesize(spoken, locale=base)
        except ProviderError as exc:
            raise HTTPException(503, error("provider_error", str(exc))) from exc
        audio = base64.b64encode(clip).decode("ascii")
        audio_format = "mp3"
    return {
        "locale": base,
        "language": speech_language(base),
        "text": spoken,
        "truncated": spoken != " ".join(body.text.split()),
        "audio": audio,
        "audio_format": audio_format,
    }


@router.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    locale: str = Form("en"),
    user: User = Depends(require_user),
) -> dict:
    """Transcribe an uploaded recording. Requires a configured STT provider;
    the browser's own speech recognition does not call this endpoint.
    """
    base = _locale_or_422(locale)
    audio = await file.read()
    content_type = file.content_type or ""
    try:
        check_audio(content_type, len(audio))
    except ValueError:
        raise HTTPException(
            422, error("invalid_audio", "Audio must be a supported type under 10MB.")
        ) from None
    try:
        transcriber = get_transcriber()
    except NotConfigured as exc:
        raise HTTPException(503, error("stt_not_configured", str(exc))) from exc
    try:
        text = await transcriber.transcribe(audio, locale=base, content_type=content_type)
    except ProviderError as exc:
        raise HTTPException(503, error("provider_error", str(exc))) from exc
    return {
        "locale": base,
        "language": speech_language(base),
        "text": text,
    }


async def _owned_conversation(
    session: AsyncSession, user: User, conversation_id: uuid.UUID
):
    row = await session.get(Conversation, conversation_id)
    if row is None or row.user_id != user.id or row.deleted_at is not None:
        raise HTTPException(
            404, error("not_found", "No conversation with that id exists for this account.")
        )
    return row


_REALTIME_MODEL = "gpt-realtime"


async def _mint_realtime_secret(locale: str) -> tuple[str, int]:
    """Mint one short-lived Realtime client secret. The long-lived key never leaves here."""
    client = AsyncOpenAI(api_key=settings.resolved_api_key, timeout=30, max_retries=2)
    try:
        created = await client.realtime.client_secrets.create(
            expires_after={"anchor": "created_at", "seconds": _REALTIME_SECRET_TTL_SECONDS},
            session={
                "type": "realtime",
                "model": _REALTIME_MODEL,
                "instructions": (
                    "You are Jarvis, a voice assistant. Reply in "
                    f"{speech_language(locale)}. When the user asks you to do something, "
                    "call submit_command with their request verbatim and a fresh "
                    "idempotency key. Do not claim an action succeeded before that tool "
                    "returns."
                ),
                "tools": [_SUBMIT_COMMAND_TOOL],
                "tool_choice": "auto",
            },
        )
    except APIError as exc:
        raise HTTPException(
            503, error("provider_error", f"openai returned {getattr(exc, 'status_code', 'unknown')}")
        ) from None
    return created.value, created.expires_at


@router.post("/realtime/session")
async def realtime_session(
    body: RealtimeSessionIn,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Mint a short-lived Realtime client secret for one owned conversation."""
    base = _locale_or_422(body.locale)
    await _owned_conversation(session, user, body.conversation_id)
    if not settings.resolved_api_key:
        raise HTTPException(
            503, error("speech_not_configured", "Speech is not configured.")
        )
    await limit_writes_durable(user.id)
    client_secret, expires_at = await _mint_realtime_secret(base)
    return {
        "provider": "openai",
        "transport": "webrtc",
        "model": _REALTIME_MODEL,
        "conversation_id": str(body.conversation_id),
        "locale": base,
        "language": speech_language(base),
        "expires_at": expires_at,
        "client_secret": client_secret,
    }


@router.post("/realtime/tool", status_code=202)
async def realtime_tool(
    body: RealtimeToolIn,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Run a spoken tool call through the same command path as typed input."""
    command = CommandIn(
        command=body.command,
        source="web",
        input_type="voice",
        locale=body.locale,
        conversation_id=body.conversation_id,
    )
    return await submit_command(command, user, session, idempotency_key=body.idempotency_key)
