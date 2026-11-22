"""Encrypt integration tokens at rest. A missing key refuses, it does not store plaintext."""

import json

from cryptography.fernet import Fernet, InvalidToken

from app.ai.base import NotConfigured
from app.config import settings


def _fernet() -> Fernet:
    if not settings.fernet_key:
        raise NotConfigured("FERNET_KEY is not set. Integration tokens cannot be stored.")
    try:
        return Fernet(settings.fernet_key.encode())
    except (ValueError, TypeError) as exc:
        raise NotConfigured("FERNET_KEY is not a valid Fernet key.") from exc



def configured() -> bool:
    """Return whether FERNET_KEY can initialize token encryption."""
    try:
        _fernet()
    except NotConfigured:
        return False
    return True


def seal(tokens: dict) -> str:
    """Encrypt a token dict. The caller never persists the dict itself."""
    raw = json.dumps(tokens, separators=(",", ":")).encode()
    return _fernet().encrypt(raw).decode()


def open_tokens(ciphertext: str) -> dict:
    try:
        raw = _fernet().decrypt(ciphertext.encode())
    except InvalidToken as exc:
        raise NotConfigured("Stored integration token could not be decrypted.") from exc
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise NotConfigured("Stored integration token is not a token object.")
    return loaded
