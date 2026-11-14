"""Voice session support. Capture stays in the browser.

This router does not receive audio and does not call a speech vendor. It tells
the browser which locale to listen in, and it turns a finished reply into the
short text the browser should speak. The command itself still goes through
POST /api/v1/assistant/commands.
"""

from fastapi import APIRouter, Depends, HTTPException
from jarvis_shared.commands import voice_reply
from jarvis_shared.speech import normalize_locale, speech_language
from pydantic import BaseModel, Field

from app.ai.speech import speech_status
from app.models import User
from app.security import error, require_user

router = APIRouter(prefix="/api/v1/voice", tags=["voice"])


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
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
    """Return the text the browser should speak. No audio is generated here."""
    base = _locale_or_422(body.locale)
    spoken = voice_reply(body.text)
    return {
        "locale": base,
        "language": speech_language(base),
        "text": spoken,
        "truncated": spoken != " ".join(body.text.split()),
        "audio": None,
    }
