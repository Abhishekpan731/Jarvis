"""Google OAuth for Gmail and Calendar. Readiness requires OAuth and token encryption.

The authorize URL is built locally. Exchanging a code, refreshing a token, and
reading a profile are the only network calls. Each refuses when the credentials
it needs are absent. A refused token request is not a missing credential.
"""

import base64
import hashlib
from urllib.parse import urlencode

import httpx
from jarvis_shared.integrations import ProviderSpec

from app.ai.base import NotConfigured, ProviderError
from app.config import settings
from app.integrations.crypto import configured as encryption_configured

_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN = "https://oauth2.googleapis.com/token"
_GMAIL_PROFILE = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
_CALENDAR_LIST = "https://www.googleapis.com/calendar/v3/users/me/calendarList"


def credentials_configured() -> bool:
    """Return whether both OAuth values are present and unpadded."""
    client_id = settings.google_client_id
    client_secret = settings.google_client_secret
    return bool(
        client_id
        and client_secret
        and client_id == client_id.strip()
        and client_secret == client_secret.strip()
    )


def configured() -> bool:
    """Return whether Google OAuth and encrypted token storage are ready."""
    return credentials_configured() and encryption_configured()


def require_configured() -> None:
    if not credentials_configured():
        raise NotConfigured("Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.")


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(spec: ProviderSpec, state: str, verifier: str) -> str:
    """Build the consent URL. Does not contact Google."""
    require_configured()
    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": " ".join(spec.scopes),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "false",
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{_AUTH}?{query}"


async def exchange_code(code: str, verifier: str) -> dict:
    """Trade an authorization code for tokens. A refusal is a provider error."""
    require_configured()
    return await _token_call(
        {
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": settings.google_redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }
    )


async def refresh(refresh_token: str) -> dict:
    require_configured()
    return await _token_call(
        {
            "refresh_token": refresh_token,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "grant_type": "refresh_token",
        }
    )


async def read_summary(provider: str, access_token: str) -> str:
    """One read-only Google call. Returns a short fact, never invented mail or events."""
    require_configured()
    if provider == "gmail":
        url = _GMAIL_PROFILE
    elif provider == "google_calendar":
        url = _CALENDAR_LIST
    else:
        raise ProviderError("That provider has no read.")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
    if response.status_code == 401:
        raise ProviderError("token_rejected")
    if response.status_code >= 400:
        raise ProviderError("google_read_failed")
    body = response.json()
    if provider == "gmail":
        address = body.get("emailAddress") or "unknown"
        count = body.get("messagesTotal")
        return f"gmail: {address}, {count} messages"
    items = body.get("items") if isinstance(body, dict) else None
    count = len(items) if isinstance(items, list) else 0
    return f"google_calendar: {count} calendars"


async def _token_call(form: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(_TOKEN, data=form)
    if response.status_code >= 400:
        # A refresh refusal is permanent. An authorization-code refusal is not a
        # missing credential; the caller maps it to a client error.
        if form.get("grant_type") == "refresh_token":
            raise ProviderError("token_rejected")
        raise ProviderError("Google refused the token request.")
    body = response.json()
    if "access_token" not in body:
        raise ProviderError("Google did not return an access token.")
    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token"),
        "expires_in": body.get("expires_in"),
        "token_type": body.get("token_type", "Bearer"),
    }
