"""Speech providers. Business code names transcribe and synthesize, never a vendor.

A missing credential is NotConfigured. A vendor failure after retries is
ProviderError. Nothing here invents a transcript or an audio clip. The browser
path does not call these at all: capture and playback stay on the device, and
only the resulting text reaches the command gateway. The transcribe/speak
endpoints are an additional, opt-in server-side path (Phase 7a).
"""

from typing import Protocol

from openai import APIError, AsyncOpenAI

from app.ai.base import NotConfigured, ProviderError
from app.config import settings


class Transcriber(Protocol):
    name: str

    async def transcribe(self, audio: bytes, *, locale: str, content_type: str) -> str: ...


class Synthesizer(Protocol):
    name: str

    async def synthesize(self, text: str, *, locale: str) -> bytes: ...


class UnconfiguredSpeech:
    """Stands in for a provider whose credential is absent. Every call raises."""

    name = "unconfigured"

    async def transcribe(self, audio: bytes, *, locale: str, content_type: str) -> str:
        raise NotConfigured("No speech-to-text credential is set.")

    async def synthesize(self, text: str, *, locale: str) -> bytes:
        raise NotConfigured("No text-to-speech credential is set.")


class OpenAITranscriber:
    """Speech-to-text via the OpenAI audio API. Request/response, not streaming."""

    name = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, timeout=30, max_retries=2)

    async def transcribe(self, audio: bytes, *, locale: str, content_type: str) -> str:
        extension = content_type.split(";", 1)[0].split("/", 1)[-1] or "webm"
        try:
            result = await self._client.audio.transcriptions.create(
                model=self.model,
                file=(f"audio.{extension}", audio, content_type),
                language=locale,
                response_format="text",
            )
        except APIError as exc:
            raise ProviderError(
                f"openai returned {getattr(exc, 'status_code', 'unknown')}"
            ) from None
        return str(result).strip()


class OpenAISynthesizer:
    """Text-to-speech via the OpenAI audio API."""

    name = "openai"

    def __init__(self, api_key: str, model: str, voice: str) -> None:
        self.model = model
        self.voice = voice
        self._client = AsyncOpenAI(api_key=api_key, timeout=30, max_retries=2)

    async def synthesize(self, text: str, *, locale: str) -> bytes:
        try:
            response = await self._client.audio.speech.create(
                model=self.model,
                voice=self.voice,
                input=text,
                response_format="mp3",
            )
        except APIError as exc:
            raise ProviderError(
                f"openai returned {getattr(exc, 'status_code', 'unknown')}"
            ) from None
        return await response.aread()


def _ready(provider: str) -> bool:
    """Whether calling the matching getter below would actually succeed."""
    return provider == "openai" and bool(settings.resolved_api_key)


def speech_status() -> dict:
    """What is configured. Browser capture does not depend on either flag."""
    return {
        "stt_configured": _ready(settings.stt_provider),
        "tts_configured": _ready(settings.tts_provider),
        "stt_provider": settings.stt_provider or None,
        "tts_provider": settings.tts_provider or None,
        "locales": ["en", "hi"],
        "browser_capture": True,
    }


def get_transcriber() -> Transcriber:
    if not settings.stt_provider:
        raise NotConfigured("No speech-to-text provider is set.")
    if settings.stt_provider != "openai":
        raise NotConfigured(f"Speech provider '{settings.stt_provider}' is not implemented.")
    if not settings.resolved_api_key:
        raise NotConfigured("No speech-to-text credential is set.")
    return OpenAITranscriber(api_key=settings.resolved_api_key, model=settings.openai_stt_model)


def get_synthesizer() -> Synthesizer:
    if not settings.tts_provider:
        raise NotConfigured("No text-to-speech provider is set.")
    if settings.tts_provider != "openai":
        raise NotConfigured(f"Speech provider '{settings.tts_provider}' is not implemented.")
    if not settings.resolved_api_key:
        raise NotConfigured("No text-to-speech credential is set.")
    return OpenAISynthesizer(
        api_key=settings.resolved_api_key,
        model=settings.openai_tts_model,
        voice=settings.openai_tts_voice,
    )
