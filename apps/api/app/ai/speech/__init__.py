"""Speech providers. Business code names transcribe and synthesize, never a vendor.

A missing credential is NotConfigured. Nothing here invents a transcript or an
audio clip. The browser path does not call these at all: capture and playback
stay on the device, and only the resulting text reaches the command gateway.
"""

from typing import Protocol

from app.ai.base import NotConfigured
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


def speech_status() -> dict:
    """What is configured. Browser capture does not depend on either flag."""
    return {
        "stt_configured": bool(settings.stt_provider),
        "tts_configured": bool(settings.tts_provider),
        "stt_provider": settings.stt_provider or None,
        "tts_provider": settings.tts_provider or None,
        "locales": ["en", "hi"],
        "browser_capture": True,
    }


def get_transcriber() -> Transcriber:
    if not settings.stt_provider:
        raise NotConfigured("No speech-to-text credential is set.")
    raise NotConfigured(f"Speech provider '{settings.stt_provider}' is not implemented.")


def get_synthesizer() -> Synthesizer:
    if not settings.tts_provider:
        raise NotConfigured("No text-to-speech credential is set.")
    raise NotConfigured(f"Speech provider '{settings.tts_provider}' is not implemented.")
