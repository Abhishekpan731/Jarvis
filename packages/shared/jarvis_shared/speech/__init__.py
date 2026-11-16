"""Speech locale rules. Pure, so tests do not need a provider or a database.

English and Hindi are the supported locales. Anything else is rejected rather
than silently transcribed as English. A missing transcript is an empty result,
never invented text.
"""

LOCALES = ("en", "hi")
_MAX_AUDIO_BYTES = 10 * 1024 * 1024
_ALLOWED_TYPES = ("audio/webm", "audio/wav", "audio/mpeg", "audio/mp4", "audio/ogg")


def normalize_locale(raw: str) -> str:
    """Map en-IN and hi-IN onto the two supported bases. Unknown locales raise."""
    cleaned = raw.strip().lower()
    if not cleaned:
        raise ValueError(raw)
    base = cleaned.split("-", 1)[0]
    if base not in LOCALES:
        raise ValueError(base or raw)
    return base


def speech_language(locale: str) -> str:
    """BCP-47 tag a browser speech engine understands."""
    base = normalize_locale(locale)
    return "hi-IN" if base == "hi" else "en-US"


def check_audio(content_type: str, size: int) -> None:
    """Reject an upload a speech provider should not see."""
    media = content_type.split(";", 1)[0].strip().lower()
    if media not in _ALLOWED_TYPES:
        raise ValueError(f"unsupported_type:{media}")
    if size <= 0 or size > _MAX_AUDIO_BYTES:
        raise ValueError("invalid_size")
